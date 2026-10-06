"""End-to-end smoke test: a real `cdp_gateway` server, a fake Chromium
(stdlib asyncio, HTTP + one hand-rolled WS connection), and a real
`websockets` client — proving the HTTP rewrite and WS proxy actually work
over real sockets, not just the pure-function unit tests in
`test_cdp_gateway.py`.

Not a replacement for `test_gateway.sh` (real Chromium, two real
playwright-mcp/omp clients) — that is the fuller integration test bauplan.md
calls for once A2's images are in. This is the fast, Docker-free check that
catches wiring mistakes (wrong websockets API, bad header handling) before
that heavier test ever has to run.
"""
import asyncio
import base64
import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest
import websockets

sys.path.insert(0, str(Path(__file__).parent))

from cdp_gateway import CdpGateway, start_server  # noqa: E402

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_frame(payload: bytes) -> bytes:
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x81, length)
    else:
        header = struct.pack("!BBH", 0x81, 126, length)
    return header + payload


def _parse_ws_frame(data: bytes) -> bytes:
    b1 = data[1]
    masked = b1 & 0x80
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack("!H", data[2:4])[0]
        offset = 4
    mask = data[offset:offset + 4] if masked else b""
    offset += 4 if masked else 0
    payload = data[offset:offset + length]
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return payload


class FakeChromium:
    """Stdlib-only fake Chromium debug port: HTTP /json/version + one
    WS connection that echoes back whatever the client sends it (good
    enough to observe Target.createTarget request/response round trips)."""

    def __init__(self):
        self.server = None
        self.port = None
        self._bad_host_seen = False
        # Every CDP method the fake was sent, in order (browser-session
        # cleanup tests check what the gateway itself asked Chromium to do).
        self.methods: list[str] = []

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        request_line = await reader.readline()
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            k, _, v = line.decode().partition(":")
            headers[k.strip().lower()] = v.strip()

        if b"GET /json/version" in request_line:
            # Real Chromium derives the host:port it reports back from the
            # REQUEST's own Host header — if it's missing the port (as a
            # naive "Host: 127.0.0.1" would be), so is the response. This
            # fake reproduces that so `test_upstream_json_request_includes_port_in_host_header`
            # actually catches a regression of the real bug found while
            # building this gateway (a bare Host header without a port made
            # the watcher's reconnect default to port 80 and loop forever).
            request_host = headers.get("host", "")
            if ":" not in request_host:
                self._bad_host_seen = True
                body = json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/abc"}).encode()
            else:
                body = json.dumps({
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/abc",
                }).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
            writer.close()
            return

        # WebSocket upgrade for /devtools/browser/abc
        key = headers["sec-websocket-key"].encode()
        accept = base64.b64encode(hashlib.sha1(key + WS_MAGIC.encode()).digest())
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        await writer.drain()

        while True:
            head = await reader.read(2)
            if len(head) < 2:
                break
            opcode = head[0] & 0x0F
            length = head[1] & 0x7F
            extra = 0
            if length == 126:
                extra = 2
            mask = await reader.read(4)
            ext = await reader.read(extra) if extra else b""
            if extra:
                length = struct.unpack("!H", ext)[0]
            payload = await reader.read(length)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close frame: ack it and stop, like a real server
                writer.write(struct.pack("!BB", 0x88, len(payload)) + payload)
                await writer.drain()
                break
            msg = json.loads(payload)
            if msg.get("method"):
                self.methods.append(msg["method"])
            # Echo a canned response for Target.createTarget specifically,
            # so the proxy's response-attribution path has something to see.
            if msg.get("method") == "Target.createTarget":
                reply = {"id": msg["id"], "result": {"targetId": "FAKE-T1"}}
            else:
                reply = {"id": msg.get("id", 0), "result": {}}
            writer.write(_ws_frame(json.dumps(reply).encode()))
            await writer.drain()
            if msg.get("method") == "Target.setDiscoverTargets":
                # Real Chromium broadcasts a targetCreated event for EVERY
                # existing/new tab to a connection with discover turned on —
                # not just tabs that connection itself creates. Puppeteer/omp
                # always sends this (bauplan.md M13), which is exactly the
                # shape that let one agent's connection misattribute another
                # agent's brand-new, unrelated tab (review finding, 03.10.2026).
                broadcast = {
                    "method": "Target.targetCreated",
                    "params": {"targetInfo": {
                        "targetId": "FOREIGN", "type": "page",
                        "title": "", "url": "https://foreign.example",
                    }},
                }
                writer.write(_ws_frame(json.dumps(broadcast).encode()))
                await writer.drain()

        writer.close()


@pytest.mark.asyncio
async def test_http_json_version_is_rewritten_with_agent_prefix():
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    async with await start_server(gateway, host="127.0.0.1", port=0) as server:
        gw_port = server.sockets[0].getsockname()[1]
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{gw_port}/a/alpha/json/version")
            data = resp.json()
            assert resp.status_code == 200
            assert data["webSocketDebuggerUrl"].startswith(f"ws://127.0.0.1:{gw_port}/a/alpha/devtools/")

    await chromium.stop()


@pytest.mark.asyncio
async def test_upstream_json_request_includes_port_in_host_header():
    """Regression guard for a real bug found while building this gateway:
    Chromium's debug HTTP server echoes back whatever host:port it was
    ASKED on in `webSocketDebuggerUrl` — a Host header without a port
    produces a portless URL, which made every later `websockets.connect()`
    off that URL default to port 80 and fail forever (live-verified against
    a real Chromium 124). `_upstream_json` must always send the port."""
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    data = await gateway._upstream_json("/json/version")
    assert not chromium._bad_host_seen, "gateway sent a Host header without a port"
    assert f":{chromium.port}" in data["webSocketDebuggerUrl"]
    await chromium.stop()


@pytest.mark.asyncio
async def test_mc_health_endpoint_503_when_watcher_not_connected():
    """bauplan.md: `/mc/health` means "the watcher connection is up", not
    merely "this process is alive" — a dead watcher silently stops tracking
    every tab (review finding: it used to always say 200, hiding exactly the
    failure mode a healthcheck exists to catch). No `run_watcher` task is
    started here, so the gateway's watcher connection is never up."""
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    async with await start_server(gateway, host="127.0.0.1", port=0) as server:
        gw_port = server.sockets[0].getsockname()[1]
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{gw_port}/mc/health")
            assert resp.status_code == 503

    await chromium.stop()


@pytest.mark.asyncio
async def test_mc_health_endpoint_200_once_the_watcher_connects():
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    stop_event = asyncio.Event()
    watcher_task = asyncio.ensure_future(gateway.run_watcher(stop_event))

    try:
        async with await start_server(gateway, host="127.0.0.1", port=0) as server:
            gw_port = server.sockets[0].getsockname()[1]
            import httpx
            async with httpx.AsyncClient() as client:
                for _ in range(50):
                    resp = await client.get(f"http://127.0.0.1:{gw_port}/mc/health")
                    if resp.status_code == 200:
                        break
                    await asyncio.sleep(0.05)
                assert resp.status_code == 200
    finally:
        stop_event.set()
        watcher_task.cancel()
        try:
            await watcher_task
        except (asyncio.CancelledError, Exception):
            pass
        await chromium.stop()


@pytest.mark.asyncio
async def test_watcher_reconnect_clears_a_tab_that_closed_during_the_outage():
    """Regression guard: a tab that closes WHILE the watcher is disconnected
    never sends `targetDestroyed` — the watcher only ever hears about it
    again via a (re)connect's fresh `targetCreated` burst, which must not
    just MERGE onto the stale state (review finding: stale entries can
    linger across a reconnect). Simulated directly against `GatewayState`,
    which is what `run_watcher` calls on every reconnect — no real sockets
    needed to prove this."""
    from cdp_gateway import GatewayState

    state = GatewayState(now_fn=lambda: 1.0)
    state.apply_target_event("targetCreated", {"targetInfo": {
        "targetId": "stale-tab", "type": "page", "title": "", "url": "https://stale.example",
    }})
    assert "stale-tab" in state.targets

    # The watcher reconnects; "stale-tab" closed in the meantime and is NOT
    # part of the fresh discover burst.
    state.reset_targets()
    state.apply_target_event("targetCreated", {"targetInfo": {
        "targetId": "still-open", "type": "page", "title": "", "url": "https://open.example",
    }})

    assert "stale-tab" not in state.targets
    assert set(state.targets) == {"still-open"}


@pytest.mark.asyncio
async def test_proxy_ws_attributes_created_target_to_identified_agent():
    """An agent connects via /a/<slug>/devtools/browser/<id>, sends
    Target.createTarget, and the gateway's state attributes the resulting
    target to that slug — the core thing B1 ships (bauplan.md M12/M13 path)."""
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    async with await start_server(gateway, host="127.0.0.1", port=0) as server:
        gw_port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{gw_port}/a/alpha/devtools/browser/abc") as client:
            await client.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}))
            raw = await asyncio.wait_for(client.recv(), timeout=5)
            reply = json.loads(raw)
            assert reply["result"]["targetId"] == "FAKE-T1"

        # Give the proxy's from_upstream task a beat to process the response
        # (it runs inside proxy_ws, inside the ws_handler coroutine, which
        # ends when `async with` above closes the connection).
        await asyncio.sleep(0.1)
        assert gateway.state.owner_of("FAKE-T1") == "alpha"

    await chromium.stop()


@pytest.mark.asyncio
async def test_proxy_ws_handler_finishes_quickly_after_client_disconnects():
    """Regression guard for a connection leak: `from_upstream` used to keep
    waiting on Chromium's still-open WebSocket long after the agent
    disconnected (`asyncio.gather` only returns once BOTH sides finish).
    Sabotage: swapping `proxy_ws`'s `asyncio.wait(..., FIRST_COMPLETED)` back
    for `asyncio.gather(t_client, t_upstream)` must flip this red
    (the fake Chromium never closes its side on its own, so the handler would
    hang until the test's own timeout)."""
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)

    handler_done = asyncio.Event()

    async def _tracked_proxy_ws(client_reader, client_writer, req, peer_ip):
        try:
            await CdpGateway.proxy_ws(gateway, client_reader, client_writer, req, peer_ip)
        finally:
            handler_done.set()

    gateway.proxy_ws = _tracked_proxy_ws  # type: ignore[method-assign]
    async with await start_server(gateway, host="127.0.0.1", port=0) as server:
        gw_port = server.sockets[0].getsockname()[1]
        client = await websockets.connect(f"ws://127.0.0.1:{gw_port}/a/alpha/devtools/browser/abc")
        # An agent that just vanishes (container killed, TCP reset) — no
        # close frame reaches Chromium, so Chromium's side stays open
        # forever. A clean `client.close()` is not enough to catch the leak
        # any more: the byte-transparent proxy forwards the close frame,
        # Chromium answers and closes, and both pumps end on their own.
        client.transport.abort()

        start = asyncio.get_event_loop().time()
        await asyncio.wait_for(handler_done.wait(), timeout=1.5)
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < 1.0, f"proxy_ws handler took {elapsed:.2f}s to finish after client close"

    await chromium.stop()


@pytest.mark.asyncio
async def test_discovering_agent_connection_does_not_attribute_a_foreign_tab():
    """Regression guard, exercised through the real `proxy_ws` path (not just
    the pure `GatewayState` unit test): an agent's connection with discover
    on receives `Target.targetCreated` for a tab it never created (the shared
    browser broadcasts every tab to every discovering connection). That must
    never make `agent` the owner of a target it did not create, has no shared
    context with, and did not open. Sabotage: restoring the removed
    "observing connection owns it" fallback (`self.state.record_owner(tid,
    agent)` for every `Target.targetCreated` in
    `_ConnectionObserver.from_upstream`) must flip this red."""
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    async with await start_server(gateway, host="127.0.0.1", port=0) as server:
        gw_port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{gw_port}/a/alpha/devtools/browser/abc") as client:
            await client.send(json.dumps({"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": True}}))
            await asyncio.wait_for(client.recv(), timeout=5)  # the ack
            await asyncio.wait_for(client.recv(), timeout=5)  # the broadcast targetCreated
        await asyncio.sleep(0.1)
        assert gateway.state.owner_of("FOREIGN") is None

    await chromium.stop()


@pytest.mark.asyncio
async def test_sabotage_wrong_agent_path_does_not_attribute_target():
    """Sabotage probe: if the path-based identification were broken (e.g.
    always returning `_shared`), this test's assertion flips — proving the
    previous test actually depends on `/a/alpha/...` being read."""
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    async with await start_server(gateway, host="127.0.0.1", port=0) as server:
        gw_port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{gw_port}/devtools/browser/abc") as client:
            await client.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}))
            await asyncio.wait_for(client.recv(), timeout=5)
        await asyncio.sleep(0.1)
        assert gateway.state.owner_of("FAKE-T1") is None

    await chromium.stop()
