"""Attribution the way omp REALLY drives the shared browser (live finding
04.10.2026: every tab showed `agent: null` in `/mc/targets`).

Three facts, all checked against the omp 18.1.10 bundle and the live
network, that the earlier B1 tests never exercised:

1. omp never creates a tab in "connected" mode. It attaches to an EXISTING
   page (`K$t(browser, {matcher, preferVisible})`), opens a CDP session on it
   (`Target.attachToTarget {flatten: true}`), sends `OMP.claimTarget` on that
   session, then `page.goto()` → `Page.navigate`. A `Target.createTarget`
   response — the only "this tab is mine" signal B1 knew — never happens, so
   nothing was ever attributed, no matter how the connection was identified.
2. Puppeteer's `connect({browserURL})` asks `new URL("/json/version",
   browserURL)` — an absolute path, so a `/a/<slug>` prefix in omp's
   `browser.cdpUrl` is DROPPED for the one request that hands out the
   browser WebSocket URL. The agent's in-container relay adds the prefix
   instead (docker/omp-bridge/cdp_relay.py); the gateway only has to honour
   it on every entry point.
3. `PUT /json/new` (Chromium's only accepted verb for it since M111) got no
   response at all — the old HTTP layer only parsed GET.

These run against a real gateway socket server and a stdlib fake Chromium,
same as test_gateway_integration.py.
"""
import asyncio
import base64
import hashlib
import json
import struct
import sys
from pathlib import Path

import httpx
import pytest
import websockets

sys.path.insert(0, str(Path(__file__).parent))

import cdp_gateway  # noqa: E402
from cdp_gateway import CdpGateway  # noqa: E402

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _frame(payload: bytes, opcode: int = 0x1) -> bytes:
    n = len(payload)
    if n < 126:
        return struct.pack("!BB", 0x80 | opcode, n) + payload
    if n < 65536:
        return struct.pack("!BBH", 0x80 | opcode, 126, n) + payload
    return struct.pack("!BBQ", 0x80 | opcode, 127, n) + payload


async def _read_frame(reader):
    head = await reader.readexactly(2)
    opcode = head[0] & 0x0F
    masked = head[1] & 0x80
    n = head[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(n)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


class FakeChromium:
    """Answers the CDP shapes omp/Puppeteer use: GET /json/version,
    GET /json/list, PUT /json/new (and Chromium's 405 for GET /json/new),
    plus browser- and page-level WebSockets that reply to
    Target.attachToTarget with a session id, to Target.createTarget with a
    target id, and with an empty result (or a CDP error for unknown methods
    like OMP.claimTarget, exactly like a real Chromium) to everything else.
    Records every request line + Host header it saw."""

    def __init__(self):
        self.port = None
        self.server = None
        self.requests: list[tuple[str, str]] = []  # (request line, host header)
        self.new_tab_counter = 0
        self.ws_messages: list[dict] = []

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        request_line = lines[0]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        self.requests.append((request_line, headers.get("host", "")))
        method, path, _ = request_line.split(" ", 2)

        def _respond(status: str, body: bytes, ctype: str = "application/json"):
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )

        if headers.get("upgrade", "").lower() == "websocket":
            await self._websocket(reader, writer, headers, path)
            return
        if path == "/json/version":
            _respond("200 OK", json.dumps({
                "Browser": "FakeChrome/1",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/abc",
            }).encode())
        elif path.startswith("/json/list") or path == "/json":
            _respond("200 OK", json.dumps([
                {"id": "P1", "type": "page", "title": "one", "url": "https://one.example",
                 "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/P1"},
            ]).encode())
        elif path.startswith("/json/new"):
            if method != "PUT":
                _respond("405 Method Not Allowed",
                         b"Using unsafe HTTP verb GET to invoke /json/new. This action supports only PUT verb.",
                         "text/plain")
            else:
                self.new_tab_counter += 1
                tid = f"NEW{self.new_tab_counter}"
                url = path.split("?", 1)[1] if "?" in path else "about:blank"
                _respond("200 OK", json.dumps({
                    "id": tid, "type": "page", "title": url, "url": url,
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/{tid}",
                }).encode())
        else:
            _respond("404 Not Found", b"Unknown command", "text/plain")
        await writer.drain()
        writer.close()

    async def _websocket(self, reader, writer, headers, path):
        key = headers["sec-websocket-key"].encode()
        accept = base64.b64encode(hashlib.sha1(key + WS_MAGIC.encode()).digest())
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        await writer.drain()
        sessions = 0
        try:
            while True:
                opcode, payload = await _read_frame(reader)
                if opcode == 0x8:
                    writer.write(_frame(payload, 0x8))
                    await writer.drain()
                    break
                if opcode != 0x1:
                    continue
                msg = json.loads(payload)
                self.ws_messages.append(msg)
                method = msg.get("method")
                reply: dict = {"id": msg.get("id", 0)}
                if "sessionId" in msg:
                    reply["sessionId"] = msg["sessionId"]
                if method == "Target.attachToTarget":
                    sessions += 1
                    reply["result"] = {"sessionId": f"S-{msg['params']['targetId']}-{sessions}"}
                elif method == "Target.createTarget":
                    reply["result"] = {"targetId": "CREATED1"}
                elif method == "OMP.claimTarget":
                    reply["error"] = {"code": -32601, "message": "'OMP.claimTarget' wasn't found"}
                else:
                    reply["result"] = {}
                writer.write(_frame(json.dumps(reply).encode()))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        writer.close()


class _Served:
    """A running gateway server on an ephemeral port."""

    def __init__(self, gateway: CdpGateway):
        self.gateway = gateway
        self.port = None
        self._server = None

    async def __aenter__(self):
        self._server = await cdp_gateway.start_server(self.gateway, host="127.0.0.1", port=0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()
        return False


async def _gateway_with_fake():
    chromium = FakeChromium()
    await chromium.start()
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=chromium.port)
    return chromium, gateway


async def _ws_call(ws, msg: dict) -> dict:
    await ws.send(json.dumps(msg))
    while True:
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if reply.get("id") == msg["id"]:
            return reply


# ── 1. omp's real attach flow: claim on use, not on create ───────────────

@pytest.mark.asyncio
async def test_omp_attach_flow_claims_the_existing_tab_for_the_prefixed_agent():
    """Exactly what omp's attach worker does on a browser-level connection:
    attachToTarget(flatten) → OMP.claimTarget on that session → Page.navigate
    on that session. No createTarget anywhere. The tab must end up owned by
    the agent named in the path prefix."""
    chromium, gateway = await _gateway_with_fake()
    async with _Served(gateway) as srv:
        async with websockets.connect(f"ws://127.0.0.1:{srv.port}/a/alpha/devtools/browser/abc") as ws:
            attach = await _ws_call(ws, {"id": 1, "method": "Target.attachToTarget",
                                         "params": {"targetId": "P1", "flatten": True}})
            sid = attach["result"]["sessionId"]
            claim = await _ws_call(ws, {"id": 2, "method": "OMP.claimTarget", "sessionId": sid})
            # Chromium rejects the unknown method — the gateway must forward
            # that error untouched (it is omp's own business), not swallow it.
            assert claim["error"]["code"] == -32601
        await asyncio.sleep(0.05)
        assert gateway.state.owner_of("P1") == "alpha"
    await chromium.stop()


@pytest.mark.asyncio
async def test_navigate_on_a_page_level_socket_claims_that_page():
    """A client that connects straight to `/devtools/page/<id>` sends its
    commands with NO sessionId — the page is named by the URL itself."""
    chromium, gateway = await _gateway_with_fake()
    async with _Served(gateway) as srv:
        async with websockets.connect(f"ws://127.0.0.1:{srv.port}/a/alpha/devtools/page/P1") as ws:
            await _ws_call(ws, {"id": 1, "method": "Page.navigate", "params": {"url": "https://example.org"}})
        await asyncio.sleep(0.05)
        assert gateway.state.owner_of("P1") == "alpha"
        # The page path reached Chromium without the agent prefix.
        assert any(line.startswith("GET /devtools/page/P1 ") for line, _ in chromium.requests)
    await chromium.stop()


@pytest.mark.asyncio
async def test_unidentified_connection_claims_nothing_sabotage_probe():
    """Same omp flow without the prefix (or a header) must leave the tab
    unassigned — proves the previous test depends on identification and
    that an anonymous client (playwright-mcp's path) never steals a tab."""
    chromium, gateway = await _gateway_with_fake()
    async with _Served(gateway) as srv:
        async with websockets.connect(f"ws://127.0.0.1:{srv.port}/devtools/browser/abc") as ws:
            attach = await _ws_call(ws, {"id": 1, "method": "Target.attachToTarget",
                                         "params": {"targetId": "P1", "flatten": True}})
            sid = attach["result"]["sessionId"]
            await _ws_call(ws, {"id": 2, "method": "OMP.claimTarget", "sessionId": sid})
            await _ws_call(ws, {"id": 3, "method": "Page.navigate", "sessionId": sid,
                                "params": {"url": "https://example.org"}})
        await asyncio.sleep(0.05)
        assert gateway.state.owner_of("P1") is None
    await chromium.stop()


# ── 2. every HTTP entry point honours the prefix ─────────────────────────

@pytest.mark.asyncio
async def test_put_json_new_through_the_prefix_creates_and_attributes_the_tab():
    chromium, gateway = await _gateway_with_fake()
    async with _Served(gateway) as srv:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.put(f"http://127.0.0.1:{srv.port}/a/alpha/json/new?https://example.com")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == "NEW1"
        assert body["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{srv.port}/a/alpha/devtools/page/NEW1"
        assert gateway.state.owner_of("NEW1") == "alpha"
        assert ("PUT /json/new?https://example.com HTTP/1.1", f"127.0.0.1:{chromium.port}") in chromium.requests
    await chromium.stop()


@pytest.mark.asyncio
async def test_get_json_new_passes_chromiums_405_through_instead_of_hanging():
    chromium, gateway = await _gateway_with_fake()
    async with _Served(gateway) as srv:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://127.0.0.1:{srv.port}/a/alpha/json/new?https://example.com")
        assert resp.status_code == 405
        assert "PUT" in resp.text
    await chromium.stop()


@pytest.mark.asyncio
async def test_unprefixed_json_version_from_puppeteer_is_not_attributed():
    """Documents fact 2 above from the gateway's side: the request Puppeteer
    itself makes carries no prefix, so the gateway can't know the agent —
    the WS URL it hands out stays unprefixed. (The relay in the agent
    container is what adds the prefix; see docker/omp-bridge/cdp_relay.py.)"""
    chromium, gateway = await _gateway_with_fake()
    async with _Served(gateway) as srv:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://127.0.0.1:{srv.port}/json/version")
        assert resp.json()["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{srv.port}/devtools/browser/abc"
    await chromium.stop()


@pytest.mark.asyncio
async def test_websocket_host_header_is_rewritten_to_an_ip_for_chromium():
    """Chromium refuses a WS upgrade whose Host is a service name ("Host
    header is specified and is not an IP address or localhost"). A client
    reaching the gateway as `cdp-browser:9300` must still get through."""
    chromium, gateway = await _gateway_with_fake()
    async with _Served(gateway) as srv:
        async with websockets.connect(
            f"ws://127.0.0.1:{srv.port}/a/alpha/devtools/browser/abc",
            additional_headers={"Host": "cdp-browser:9300"},
        ) as ws:
            await _ws_call(ws, {"id": 1, "method": "Browser.getVersion"})
        ws_requests = [(line, host) for line, host in chromium.requests if "/devtools/browser/abc" in line]
        assert ws_requests == [("GET /devtools/browser/abc HTTP/1.1", f"127.0.0.1:{chromium.port}")]
    await chromium.stop()


@pytest.mark.asyncio
async def test_large_messages_pass_through_byte_for_byte():
    """The proxy must stay transparent for big CDP payloads (screenshots are
    multi-MB base64) — the sniffer skips them, the bytes still arrive."""
    chromium, gateway = await _gateway_with_fake()
    big = "x" * (3 * 1024 * 1024)
    async with _Served(gateway) as srv:
        async with websockets.connect(
            f"ws://127.0.0.1:{srv.port}/a/alpha/devtools/browser/abc", max_size=16 * 1024 * 1024,
        ) as ws:
            reply = await _ws_call(ws, {"id": 7, "method": "Runtime.evaluate", "params": {"expression": big}})
            assert reply == {"id": 7, "result": {}}
        assert chromium.ws_messages[-1]["params"]["expression"] == big
    await chromium.stop()


# ── review follow-ups ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_head_request_is_answered_without_waiting_for_a_body():
    """A HEAD answer carries Content-Length but no body; reading one used to
    wait 10 s and end in a 502."""
    chromium, gateway = await _gateway_with_fake()

    async def _head_aware(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        chromium.requests.append((head.split(b"\r\n", 1)[0].decode(), ""))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 42\r\n\r\n")
        await writer.drain()  # no body: it's a HEAD answer
        await asyncio.sleep(5)
        writer.close()

    chromium.server.close()
    await chromium.server.wait_closed()
    chromium.server = await asyncio.start_server(_head_aware, "127.0.0.1", chromium.port)
    async with _Served(gateway) as srv:
        async with httpx.AsyncClient(timeout=5) as client:
            start = asyncio.get_event_loop().time()
            resp = await client.head(f"http://127.0.0.1:{srv.port}/a/alpha/json/version")
            elapsed = asyncio.get_event_loop().time() - start
        assert resp.status_code == 200
        assert elapsed < 2.0, f"HEAD took {elapsed:.1f}s"
    await chromium.stop()


@pytest.mark.asyncio
async def test_sessions_opened_on_a_connection_are_forgotten_when_it_closes():
    chromium, gateway = await _gateway_with_fake()
    async with _Served(gateway) as srv:
        async with websockets.connect(f"ws://127.0.0.1:{srv.port}/a/alpha/devtools/browser/abc") as ws:
            attach = await _ws_call(ws, {"id": 1, "method": "Target.attachToTarget",
                                         "params": {"targetId": "P1", "flatten": True}})
            sid = attach["result"]["sessionId"]
            await asyncio.sleep(0.05)
            assert gateway.state.session_target.get(sid) == "P1"
        for _ in range(40):
            if sid not in gateway.state.session_target:
                break
            await asyncio.sleep(0.05)
        assert sid not in gateway.state.session_target
    await chromium.stop()


@pytest.mark.asyncio
async def test_vanished_agent_ends_the_proxy_even_if_chromium_ignores_the_half_close():
    """The other direction only gets a short grace after one side ends — an
    upstream that neither answers nor closes must not keep the handler (and
    a browser-level CDP session) alive."""
    accepted = asyncio.Event()

    async def _stubborn(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        key = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + WS_MAGIC.encode()).digest())
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        await writer.drain()
        accepted.set()
        await asyncio.sleep(30)  # never reads, never closes

    upstream = await asyncio.start_server(_stubborn, "127.0.0.1", 0)
    gateway = CdpGateway(upstream_host="127.0.0.1", upstream_port=upstream.sockets[0].getsockname()[1])
    done = asyncio.Event()
    original = CdpGateway.proxy_ws

    async def _tracked(*args, **kwargs):
        try:
            await original(gateway, *args, **kwargs)
        finally:
            done.set()

    gateway.proxy_ws = _tracked  # type: ignore[method-assign]
    async with _Served(gateway) as srv:
        client = await websockets.connect(f"ws://127.0.0.1:{srv.port}/a/alpha/devtools/browser/abc")
        await accepted.wait()
        client.transport.abort()
        start = asyncio.get_event_loop().time()
        await asyncio.wait_for(done.wait(), timeout=3)
        assert asyncio.get_event_loop().time() - start < 1.5
    upstream.close()
