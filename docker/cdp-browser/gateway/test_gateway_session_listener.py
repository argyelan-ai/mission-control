"""The gateway's host-published listener serves browser sessions only.

The internal listener (9300 inside the compose network) answers every shape:
unprefixed CDP, `/a/<slug>/`, `/mc/*`. Published on the host's loopback, that
would hand the whole shared browser to any page the operator opens: a DNS
rebinding page (Host: rebind.example, resolving to 127.0.0.1) could reach it,
because the gateway rewrites the Host header Chromium would otherwise check.
So the published port is a second listener (`CDP_GATEWAY_SESSION_PORT`) that
serves only `/s/<token>/…` (never `/s/<token>/mc/…`), only for a local Host
header, and refuses any non-local Origin.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent))

from cdp_gateway import CdpGateway, local_host, start_server  # noqa: E402

TOKEN = "listener-token-" + "t" * 30


class FakeUpstream:
    """Chromium's debug HTTP port, just enough for /json/version."""

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = json.dumps({"Browser": "Fake", "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/x"}).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


@pytest_asyncio.fixture
async def listeners():
    up = FakeUpstream()
    await up.start()
    gw = CdpGateway(upstream_host="127.0.0.1", upstream_port=up.port)
    gw.state.register_session(TOKEN, "33333333-3333-4333-8333-333333333333", None)
    internal = await start_server(gw, "127.0.0.1", 0)
    published = await start_server(gw, "127.0.0.1", 0, sessions_only=True)
    ports = {
        "internal": internal.sockets[0].getsockname()[1],
        "published": published.sockets[0].getsockname()[1],
    }
    yield ports
    for server in (internal, published):
        server.close()
        await server.wait_closed()
    await up.stop()


async def _get(port, path, headers=None, upgrade=False):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"GET {path} HTTP/1.1"]
    hdrs = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    if upgrade:
        hdrs.update({"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                     "Sec-WebSocket-Key": "AAAAAAAAAAAAAAAAAAAAAA=="})
    lines += [f"{k}: {v}" for k, v in hdrs.items() if v is not None]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    writer.close()
    return int(head.split(b" ", 2)[1])


@pytest.mark.asyncio
async def test_published_listener_serves_a_registered_session(listeners):
    assert await _get(listeners["published"], f"/s/{TOKEN}/json/version") == 200


@pytest.mark.asyncio
async def test_published_listener_refuses_everything_but_session_addresses(listeners):
    port = listeners["published"]
    for path in ("/json/version", "/json/list", "/a/alpha/json/version", "/mc/targets", "/mc/sessions",
                 "/mc/health", f"/s/{TOKEN}/mc/targets", f"/s/{TOKEN}/mc/sessions", "/devtools/browser/x"):
        assert await _get(port, path) == 404, path
    assert await _get(port, "/devtools/browser/x", upgrade=True) == 404
    assert await _get(port, "/a/alpha/devtools/browser/x", upgrade=True) == 404


@pytest.mark.asyncio
async def test_published_listener_refuses_a_rebinding_host(listeners):
    """DNS rebinding: a page on rebind.example resolved to 127.0.0.1 sends
    its own name as Host."""
    port = listeners["published"]
    for host in ("rebind.attacker.example", "rebind.attacker.example:9300", "localhost.attacker.example", "", None):
        assert await _get(port, f"/s/{TOKEN}/json/version", {"Host": host}) in (400, 403), host
    assert await _get(port, f"/s/{TOKEN}/devtools/browser/x", {"Host": "rebind.attacker.example"}, upgrade=True) == 403
    for host in ("127.0.0.1:9300", "localhost:9300", "[::1]:9300", "cdp-browser:9300", "172.30.99.10:9300"):
        assert await _get(port, f"/s/{TOKEN}/json/version", {"Host": host}) == 200, host


@pytest.mark.asyncio
async def test_published_listener_refuses_a_foreign_origin(listeners):
    port = listeners["published"]
    for origin in ("http://rebind.attacker.example", "https://example.org", "null", "chrome-extension://abc"):
        assert await _get(port, f"/s/{TOKEN}/json/version", {"Origin": origin}) == 403, origin
    assert await _get(port, f"/s/{TOKEN}/devtools/browser/x", {"Origin": "http://evil.example"}, upgrade=True) == 403
    for origin in ("http://localhost:3000", "http://127.0.0.1:8080"):
        assert await _get(port, f"/s/{TOKEN}/json/version", {"Origin": origin}) == 200, origin


@pytest.mark.asyncio
async def test_internal_listener_is_unchanged(listeners):
    port = listeners["internal"]
    assert await _get(port, "/json/version") == 200
    assert await _get(port, "/json/version", {"Host": "cdp-browser:9300"}) == 200


def test_local_host_rule():
    for ok in ("127.0.0.1", "127.0.0.1:9300", "localhost:1", "LOCALHOST", "[::1]:9300", "cdp-browser", "10.0.0.5:9300"):
        assert local_host(ok), ok
    for bad in ("", None, "example.org", "rebind.example:9300", "[::1", "cdp-browser.evil.example"):
        assert not local_host(bad), bad
