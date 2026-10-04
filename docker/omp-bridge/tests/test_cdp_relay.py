"""cdp_relay.py — the in-container CDP relay that tells cdp-gateway which
agent is talking (live finding 04.10.2026).

Why it exists: omp attaches with Puppeteer's `connect({browserURL})`, which
asks `new URL("/json/version", browserURL)` — an ABSOLUTE path, so a
`/a/<slug>` prefix in omp's `browser.cdpUrl` is dropped for exactly the
request that hands out the browser WebSocket URL, and reverse DNS of the
container failed in the real network. The relay puts the prefix on every
request line itself, so attribution no longer depends on what the client
library does with the URL.

The integration tests run the relay against a fake upstream over real
sockets and check exactly which request lines arrive there.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct

import pytest

import cdp_relay

WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ── pure helpers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "line, expected",
    [
        (b"GET /json/version HTTP/1.1", b"GET /a/alpha/json/version HTTP/1.1"),
        (b"PUT /json/new?https://example.com/x HTTP/1.1", b"PUT /a/alpha/json/new?https://example.com/x HTTP/1.1"),
        (b"GET /devtools/browser/abc HTTP/1.1", b"GET /a/alpha/devtools/browser/abc HTTP/1.1"),
        (b"GET /devtools/page/P1 HTTP/1.1", b"GET /a/alpha/devtools/page/P1 HTTP/1.1"),
        # Already prefixed for this agent (omp's own cdpUrl, or a WS URL the
        # gateway handed out): unchanged, never doubled.
        (b"GET /a/alpha/json/version HTTP/1.1", b"GET /a/alpha/json/version HTTP/1.1"),
        (b"GET /a/alpha/devtools/browser/abc HTTP/1.1", b"GET /a/alpha/devtools/browser/abc HTTP/1.1"),
        # A different agent's prefix from inside THIS container is a stale or
        # copied URL — the relay speaks for its own agent only.
        (b"GET /a/beta/json/version HTTP/1.1", b"GET /a/alpha/json/version HTTP/1.1"),
        (b"GET / HTTP/1.1", b"GET /a/alpha/ HTTP/1.1"),
    ],
)
def test_prefix_request_line(line, expected):
    assert cdp_relay.prefix_request_line(line, "/a/alpha") == expected


@pytest.mark.parametrize("line", [b"garbage", b"GET http://x/ HTTP/1.1", b"CONNECT a:1 HTTP/1.1", b""])
def test_prefix_request_line_leaves_non_origin_form_alone(line):
    assert cdp_relay.prefix_request_line(line, "/a/alpha") == line


def test_agent_path_on_for_the_gateway_port(monkeypatch):
    monkeypatch.delenv("OMP_BROWSER_CDP_ATTRIBUTION", raising=False)
    monkeypatch.setenv("OMP_BROWSER_CDP_TARGET", "cdp-browser:9300")
    monkeypatch.setenv("AGENT_SLUG", "alpha")
    assert cdp_relay.agent_path() == "/a/alpha"


def test_agent_path_falls_back_to_agent_name(monkeypatch):
    monkeypatch.delenv("OMP_BROWSER_CDP_ATTRIBUTION", raising=False)
    monkeypatch.delenv("AGENT_SLUG", raising=False)
    monkeypatch.setenv("OMP_BROWSER_CDP_TARGET", "cdp-browser:9300")
    monkeypatch.setenv("AGENT_NAME", "Alpha")
    assert cdp_relay.agent_path() == "/a/alpha"


def test_agent_path_off_for_the_plain_chromium_port(monkeypatch):
    """`cdp-browser:9223` is the documented rollback (bypass the gateway):
    Chromium itself would answer `/a/<slug>/json/version` with 404."""
    monkeypatch.delenv("OMP_BROWSER_CDP_ATTRIBUTION", raising=False)
    monkeypatch.setenv("OMP_BROWSER_CDP_TARGET", "cdp-browser:9223")
    monkeypatch.setenv("AGENT_SLUG", "alpha")
    assert cdp_relay.agent_path() == ""


@pytest.mark.parametrize("mode, target, expected", [
    ("on", "browser.example:7000", "/a/alpha"),
    ("off", "cdp-browser:9300", ""),
    ("auto", "cdp-browser:9300", "/a/alpha"),
])
def test_agent_path_explicit_mode(monkeypatch, mode, target, expected):
    monkeypatch.setenv("OMP_BROWSER_CDP_ATTRIBUTION", mode)
    monkeypatch.setenv("OMP_BROWSER_CDP_TARGET", target)
    monkeypatch.setenv("AGENT_SLUG", "alpha")
    assert cdp_relay.agent_path() == expected


@pytest.mark.parametrize("slug", ["", "../etc", "Has Space", "-lead", "a" * 70])
def test_agent_path_refuses_a_slug_the_gateway_would_reject(monkeypatch, slug):
    monkeypatch.setenv("OMP_BROWSER_CDP_ATTRIBUTION", "on")
    monkeypatch.setenv("OMP_BROWSER_CDP_TARGET", "cdp-browser:9300")
    monkeypatch.setenv("AGENT_SLUG", slug)
    monkeypatch.delenv("AGENT_NAME", raising=False)
    assert cdp_relay.agent_path() == ""


# ── over real sockets ─────────────────────────────────────────────────────

class FakeUpstream:
    """Records every request head; answers HTTP with a tiny JSON body (kept
    alive, like Chromium) and WebSocket upgrades with 101 + an echo loop."""

    def __init__(self):
        self.request_lines: list[bytes] = []
        self.ws_payloads: list[bytes] = []
        self.port = None
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                first, _, rest = head.partition(b"\r\n")
                self.request_lines.append(first)
                headers = {
                    k.strip().lower(): v.strip()
                    for k, _, v in (line.partition(b":") for line in rest.split(b"\r\n") if line)
                }
                length = int(headers.get(b"content-length", b"0"))
                if length:
                    await reader.readexactly(length)
                if headers.get(b"upgrade", b"").lower() == b"websocket":
                    accept = base64.b64encode(hashlib.sha1(headers[b"sec-websocket-key"] + WS_MAGIC).digest())
                    writer.write(
                        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                        b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                    )
                    await writer.drain()
                    while True:
                        data = await reader.read(65536)
                        if not data:
                            return
                        self.ws_payloads.append(data)
                        writer.write(data)  # echo raw bytes back
                        await writer.drain()
                body = b'{"ok": true}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _start_relay(upstream_port: int, agent_path: str):
    return await cdp_relay.start_relay("127.0.0.1", 0, "127.0.0.1", upstream_port, agent_path)


async def _http(port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    length = int([ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")][0].split(b":")[1])
    body = await reader.readexactly(length)
    writer.close()
    return head + body


@pytest.mark.asyncio
async def test_every_request_on_a_keep_alive_connection_gets_the_prefix():
    """Bun's fetch (omp) reuses connections — rewriting only the FIRST
    request would leave the second one anonymous."""
    upstream = FakeUpstream()
    await upstream.start()
    relay = await _start_relay(upstream.port, "/a/alpha")
    port = relay.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for path in (b"/json/version", b"/json/list"):
            writer.write(b"GET " + path + b" HTTP/1.1\r\nHost: 127.0.0.1:9222\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 200")
            await reader.readexactly(12)
        writer.close()
        assert upstream.request_lines == [
            b"GET /a/alpha/json/version HTTP/1.1",
            b"GET /a/alpha/json/list HTTP/1.1",
        ]
    finally:
        relay.close()
        await relay.wait_closed()
        await upstream.stop()


@pytest.mark.asyncio
async def test_put_with_a_body_keeps_framing_and_the_next_request_is_still_prefixed():
    upstream = FakeUpstream()
    await upstream.start()
    relay = await _start_relay(upstream.port, "/a/alpha")
    port = relay.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"PUT /json/new?https://example.com HTTP/1.1\r\nHost: 127.0.0.1:9222\r\n"
            b"Content-Length: 5\r\n\r\nhello"
            b"GET /json/version HTTP/1.1\r\nHost: 127.0.0.1:9222\r\n\r\n"
        )
        await writer.drain()
        for _ in range(2):
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            await reader.readexactly(12)
        writer.close()
        assert upstream.request_lines == [
            b"PUT /a/alpha/json/new?https://example.com HTTP/1.1",
            b"GET /a/alpha/json/version HTTP/1.1",
        ]
    finally:
        relay.close()
        await relay.wait_closed()
        await upstream.stop()


@pytest.mark.asyncio
async def test_websocket_frames_after_the_upgrade_pass_through_untouched():
    """After `101 Switching Protocols` the stream is WebSocket frames, not
    HTTP: a frame that happens to look like a request line must never be
    rewritten."""
    upstream = FakeUpstream()
    await upstream.start()
    relay = await _start_relay(upstream.port, "/a/alpha")
    port = relay.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # A fresh random handshake key (a fixed sample key trips gitleaks).
        ws_key = base64.b64encode(os.urandom(16))
        writer.write(
            b"GET /devtools/browser/abc HTTP/1.1\r\nHost: 127.0.0.1:9222\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: " + ws_key + b"\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert head.startswith(b"HTTP/1.1 101")
        tricky = b"GET /json/version HTTP/1.1\r\n\r\n"
        frame = struct.pack("!BB", 0x81, len(tricky)) + tricky
        writer.write(frame)
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(len(frame)), 5)
        assert echoed == frame
        writer.close()
        assert upstream.request_lines == [b"GET /a/alpha/devtools/browser/abc HTTP/1.1"]
        assert b"".join(upstream.ws_payloads) == frame
    finally:
        relay.close()
        await relay.wait_closed()
        await upstream.stop()


@pytest.mark.asyncio
async def test_without_an_agent_path_the_relay_is_a_plain_pipe_sabotage_probe():
    """Same request, attribution off: the line arrives exactly as sent —
    proves the prefix in the tests above comes from the relay's rewrite."""
    upstream = FakeUpstream()
    await upstream.start()
    relay = await _start_relay(upstream.port, "")
    port = relay.sockets[0].getsockname()[1]
    try:
        resp = await _http(port, b"GET /json/version HTTP/1.1\r\nHost: 127.0.0.1:9222\r\n\r\n")
        assert resp.startswith(b"HTTP/1.1 200")
        assert upstream.request_lines == [b"GET /json/version HTTP/1.1"]
    finally:
        relay.close()
        await relay.wait_closed()
        await upstream.stop()


@pytest.mark.asyncio
async def test_upstream_down_closes_the_client_instead_of_hanging():
    relay = await _start_relay(1, "/a/alpha")  # nothing listens on port 1
    port = relay.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /json/version HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
    finally:
        relay.close()
        await relay.wait_closed()
