#!/usr/bin/env python3
"""cdp-gateway — agent-aware front door to the shared cdp-browser Chromium.

Runs inside the `cdp-browser` container next to Chromium and the existing
socat forwarder (port 9223, unchanged — the backend's screencast path keeps
using it exactly as before). This gateway listens on port 9300 and answers
the same shapes Chromium's own debug port does (`/json/version`, `/json/list`,
`/json/new`) plus a WebSocket proxy to the real browser — but it also figures
out WHICH AGENT a connection belongs to, so the operator's per-agent panel
can show "Sparky's tabs" instead of "every tab in the shared browser"
(bauplan.md, PR B1).

Scope of THIS module (B1 only): identify the agent behind a connection and
track target ownership. It does **not** stop one agent from seeing or
touching another agent's tabs yet — that is PR B3's job
(`CDP_GATEWAY_ISOLATE`), built on top of the ownership map this module
produces. Shipping identification and isolation separately keeps each PR
small enough to review and revert on its own (bauplan.md §4).

Identification order for an inbound connection (bauplan.md §3):
  1. URL path prefix `/a/<slug>/...` (Playwright keeps the full CDP endpoint
     path it was given — live-verified, bauplan M12).
  2. Header `X-MC-Agent: <slug>`.
  3. Reverse DNS of the peer IP -> `mc-agent-<slug>.<network>` -> `<slug>`
     (omp/Puppeteer drops both the path and the header — M13; the result is
     cached for `_REVERSE_DNS_CACHE_SECONDS` since a reverse lookup per frame
     would be wasteful and omp's relay keeps one long-lived TCP connection
     anyway).
  4. Otherwise `_shared` (unidentified — behaves exactly like today).

A slug that doesn't match `_SLUG_RE` is treated as unidentified (`_shared`)
rather than trusted verbatim: this value ends up in HTTP paths, WS URLs and
(indirectly) in `/mc/targets` JSON shown to the operator, so a stray `..` or
quote must never reach any of those unescaped.

Known gap, not a blocker: `PUT /json/new` (Chromium's HTTP convenience endpoint
for opening a tab) is not proxied — `websockets.asyncio.server`'s HTTP support
only parses GET (it is an opening-handshake parser, not a general HTTP
server; verified against a real Chromium 124 while building this: a PUT here
gets no response at all, connection just closes). This does not block real
usage: Playwright and Puppeteer (and therefore omp, M14) create tabs via
`Target.createTarget` over the already-proxied WebSocket connection, never
via this HTTP endpoint — `proxy_ws` handles that path and is what attributes
a newly-created tab to the agent that created it. A future PR that needs the
HTTP endpoint too (e.g. a curl-based tooling path) would need to replace this
module's HTTP layer with a general-purpose one (raw asyncio streams, or
aiohttp once the base image situation from A2 settles) — tracked, not done
here, to keep this PR's diff to what B1 actually needs.

Trust model: identification is NOT authentication. Any container on the
Docker-internal `mission-control_default` network can already reach
Chromium's CDP port directly (today, via :9223) — this gateway does not
change that boundary, it only labels connections for the UI. A malicious
container on the same network could still claim to be any agent by sending
a matching path or header; that is an accepted limitation, consistent with
every other agent-to-agent trust assumption in this stack (same network =
same trust tier) until B3's isolation + a real per-agent credential (not
scoped here) exist.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import websockets
from websockets.asyncio.server import serve as ws_serve

logger = logging.getLogger("cdp_gateway")

CHROMIUM_HOST = os.environ.get("CDP_GATEWAY_UPSTREAM_HOST", "127.0.0.1")
CHROMIUM_PORT = int(os.environ.get("CDP_GATEWAY_UPSTREAM_PORT", "9222"))
LISTEN_PORT = int(os.environ.get("CDP_GATEWAY_PORT", "9300"))

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHARED = "_shared"
_REVERSE_DNS_CACHE_SECONDS = 60.0
_BLANK_URLS = {"about:blank", ""}


def normalize_slug(raw: Optional[str]) -> Optional[str]:
    """Returns the slug lowercased if it matches the allowed shape, else
    None (caller falls through to the next identification method)."""
    if not raw:
        return None
    candidate = raw.strip().lower()
    return candidate if _SLUG_RE.match(candidate) else None


def slug_from_path(path: str) -> Optional[str]:
    """`/a/<slug>/json/version` -> `<slug>`. Also matches the bare
    `/a/<slug>` and `/a/<slug>/devtools/...` shapes."""
    parts = path.lstrip("/").split("/")
    if len(parts) >= 2 and parts[0] == "a":
        return normalize_slug(parts[1])
    return None


def strip_agent_prefix(path: str) -> str:
    """`/a/<slug>/json/version` -> `/json/version`; leaves other paths as-is
    (the legacy, unprefixed shape stays valid — callers without a slug keep
    working exactly like talking to Chromium directly)."""
    parts = path.lstrip("/").split("/", 2)
    if len(parts) >= 2 and parts[0] == "a" and normalize_slug(parts[1]):
        return "/" + (parts[2] if len(parts) > 2 else "")
    return path


class _ReverseDnsCache:
    def __init__(self, *, now_fn=time.monotonic, resolver=None):
        self._entries: dict[str, tuple[float, Optional[str]]] = {}
        self._now = now_fn
        # Injectable for tests; production uses the real (blocking) stdlib
        # call off the event loop via asyncio.to_thread.
        self._resolver = resolver or socket.gethostbyaddr

    async def lookup_slug(self, ip: str) -> Optional[str]:
        now = self._now()
        cached = self._entries.get(ip)
        if cached is not None and now - cached[0] < _REVERSE_DNS_CACHE_SECONDS:
            return cached[1]
        slug = await self._resolve(ip)
        self._entries[ip] = (now, slug)
        return slug

    async def _resolve(self, ip: str) -> Optional[str]:
        try:
            host, _aliases, _addrs = await asyncio.to_thread(self._resolver, ip)
        except OSError:
            return None
        # "mc-agent-sparky.mission-control_default" -> "sparky" (M13).
        short = host.split(".", 1)[0]
        if not short.startswith("mc-agent-"):
            return None
        return normalize_slug(short[len("mc-agent-"):])


@dataclass
class TargetInfo:
    id: str
    title: str = ""
    url: str = ""
    created_at: float = 0.0
    last_active_at: float = 0.0
    agent: Optional[str] = None


@dataclass
class GatewayState:
    """Everything this module tracks about open pages and who owns them.
    Pure, synchronous and dependency-free on purpose — this is the part a
    unit test drives directly, without sockets (bauplan.md's `test_cdp_gateway.py`).
    """

    targets: dict[str, TargetInfo] = field(default_factory=dict)
    ctx_owner: dict[str, str] = field(default_factory=dict)
    target_owner: dict[str, str] = field(default_factory=dict)
    session_target: dict[str, str] = field(default_factory=dict)
    now_fn: callable = field(default=time.monotonic)

    # ── identification plumbing used by both HTTP and WS entry points ─────

    def owner_of(self, target_id: str) -> Optional[str]:
        return self.target_owner.get(target_id)

    def record_owner(self, target_id: str, agent: Optional[str]) -> None:
        if agent and agent != _SHARED:
            self.target_owner[target_id] = agent
            info = self.targets.get(target_id)
            if info is not None:
                info.agent = agent

    def record_context_owner(self, context_id: str, agent: Optional[str]) -> None:
        if agent and agent != _SHARED:
            self.ctx_owner[context_id] = agent

    # ── target lifecycle (fed by Target.* events on the watcher conn) ─────

    def apply_target_event(self, method: str, params: dict) -> bool:
        now = self.now_fn()
        if method == "targetCreated":
            info = params.get("targetInfo", {})
            if info.get("type") != "page":
                return False
            tid = info["targetId"]
            ctx = info.get("browserContextId")
            agent = self.target_owner.get(tid) or (self.ctx_owner.get(ctx) if ctx else None)
            self.targets[tid] = TargetInfo(
                id=tid,
                title=info.get("title", ""),
                url=info.get("url", ""),
                created_at=now,
                last_active_at=now,
                agent=agent,
            )
            return True
        if method == "targetInfoChanged":
            info = params.get("targetInfo", {})
            if info.get("type") != "page":
                return False
            tid = info["targetId"]
            existing = self.targets.get(tid)
            if existing is None:
                self.targets[tid] = TargetInfo(
                    id=tid, title=info.get("title", ""), url=info.get("url", ""),
                    created_at=now, last_active_at=now,
                    agent=self.target_owner.get(tid),
                )
                return True
            url_changed = existing.url != info.get("url", existing.url)
            existing.title = info.get("title", existing.title)
            existing.url = info.get("url", existing.url)
            if url_changed:
                existing.last_active_at = now
            return url_changed
        if method == "targetDestroyed":
            tid = params.get("targetId")
            if tid in self.targets:
                del self.targets[tid]
                self.target_owner.pop(tid, None)
                return True
            return False
        return False

    def mark_active_by_session(self, session_id: str) -> None:
        """A command sent WITH a sessionId is the agent working in that tab
        right now — this is how a plain tab-switch (which emits no
        Target.* event, M11) still counts as activity."""
        tid = self.session_target.get(session_id)
        if tid and tid in self.targets:
            self.targets[tid].last_active_at = self.now_fn()

    def observe_response(self, request_method: str, params: dict, result: dict, agent: Optional[str]) -> None:
        """Feed the result of a Target.* command this agent issued, so later
        traffic from/about the resulting context or target can be attributed."""
        if agent is None or agent == _SHARED:
            return
        if request_method == "Target.createBrowserContext":
            ctx = result.get("browserContextId")
            if ctx:
                self.record_context_owner(ctx, agent)
        elif request_method == "Target.createTarget":
            tid = result.get("targetId")
            if tid:
                self.record_owner(tid, agent)
        elif request_method == "Target.attachToTarget":
            session_id = result.get("sessionId")
            tid = params.get("targetId")
            if session_id and tid:
                self.session_target[session_id] = tid

    def observe_event(self, method: str, params: dict) -> None:
        if method == "Target.attachedToTarget":
            session_id = params.get("sessionId")
            tid = (params.get("targetInfo") or {}).get("targetId")
            if session_id and tid:
                self.session_target[session_id] = tid

    # ── read side for /mc/targets and per-agent /json/list ─────────────────

    def targets_for(self, agent: Optional[str]) -> list[TargetInfo]:
        values = list(self.targets.values())
        if agent is not None:
            values = [t for t in values if t.agent == agent]
        return sorted(values, key=lambda t: t.last_active_at, reverse=True)

    def as_mc_targets_json(self, agent: Optional[str] = None) -> list[dict]:
        return [
            {
                "targetId": t.id,
                "title": t.title,
                "url": t.url,
                "agent": t.agent,
                "createdAt": t.created_at,
                "lastActiveAt": t.last_active_at,
            }
            for t in self.targets_for(agent)
        ]


def identify_agent_sync(
    *, path: str, headers, peer_ip: Optional[str]
) -> tuple[Optional[str], str]:
    """The synchronous (path + header) half of identification — split out
    from the async reverse-DNS step so it's trivially unit-testable without
    an event loop. Returns (slug_or_None, method_name)."""
    by_path = slug_from_path(path)
    if by_path:
        return by_path, "path"
    by_header = normalize_slug(headers.get("X-MC-Agent"))
    if by_header:
        return by_header, "header"
    return None, "none"


class CdpGateway:
    def __init__(self, *, upstream_host: str = CHROMIUM_HOST, upstream_port: int = CHROMIUM_PORT):
        self.state = GatewayState()
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._dns_cache = _ReverseDnsCache()
        self._next_gateway_id = 2_000_000_000  # reserved range, bauplan.md §3

    async def identify(self, path: str, headers, peer_ip: Optional[str]) -> str:
        slug, _method = identify_agent_sync(path=path, headers=headers, peer_ip=peer_ip)
        if slug:
            return slug
        if peer_ip:
            try:
                ipaddress.ip_address(peer_ip)
            except ValueError:
                peer_ip = None
        if peer_ip:
            by_dns = await self._dns_cache.lookup_slug(peer_ip)
            if by_dns:
                return by_dns
        return _SHARED

    # ── HTTP side: /json/version, /json/list, /mc/targets, /mc/health ─────

    async def _upstream_json(self, path: str) -> dict:
        """GET a JSON endpoint off Chromium's debug port. Reads exactly
        `Content-Length` bytes rather than "until EOF": Chromium's debug
        HTTP server keeps the connection alive regardless of the
        `Connection: close` request header (live-verified against a real
        Chromium 124 — `reader.read()` with no size bound simply hung
        forever here, because EOF never came), so a bound-less read would
        hang every single call through this gateway, not just the
        watcher's."""
        reader, writer = await asyncio.open_connection(self._upstream_host, self._upstream_port)
        try:
            # Chromium's debug HTTP server derives the host:port it reports
            # back in `webSocketDebuggerUrl` from the Host header of the
            # VERY REQUEST asking for it (live-verified against a real
            # Chromium 124: a bare "Host: 127.0.0.1" with no port got back
            # "ws://127.0.0.1/devtools/..." — no port at all, which then
            # made `websockets.connect()` default to port 80 and fail
            # forever). Always send the port, even though it's "our own"
            # upstream port and the response is about to be discarded by
            # every caller except the two that read `webSocketDebuggerUrl`
            # (`handle_http`'s rewrite and `run_watcher`'s own connect).
            req = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {self._upstream_host}:{self._upstream_port}\r\n"
                f"Connection: close\r\n\r\n"
            )
            writer.write(req.encode())
            await writer.drain()
            header_blob = await reader.readuntil(b"\r\n\r\n")
            content_length = 0
            for line in header_blob.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
                    break
            body = await reader.readexactly(content_length) if content_length else await reader.read()
        finally:
            writer.close()
        return json.loads(body or b"{}")

    async def handle_http(self, path: str, headers, peer_ip: Optional[str]):
        """Returns (status, content_type, body_bytes) for a plain HTTP GET.
        Caller (the websockets server's process_request hook) turns this
        into an actual HTTP response."""
        if path in ("/mc/health",):
            return 200, "text/plain", b"ok"
        if path.startswith("/mc/targets"):
            agent = None
            if "?" in path:
                from urllib.parse import parse_qs
                qs = parse_qs(path.split("?", 1)[1])
                agent = (qs.get("agent") or [None])[0]
                agent = normalize_slug(agent) if agent else None
            body = json.dumps(self.state.as_mc_targets_json(agent)).encode()
            return 200, "application/json", body

        agent = await self.identify(path, headers, peer_ip)
        upstream_path = strip_agent_prefix(path)
        try:
            data = await self._upstream_json(upstream_path)
        except OSError as e:
            return 502, "text/plain", f"cdp-gateway: upstream unreachable: {e}".encode()

        host_header = headers.get("Host", f"127.0.0.1:{LISTEN_PORT}")
        prefix = f"/a/{agent}" if agent != _SHARED else ""

        def _rewrite(obj):
            if isinstance(obj, dict):
                for key in ("webSocketDebuggerUrl",):
                    if key in obj and obj[key]:
                        parsed = urlparse(obj[key])
                        new_path = prefix + parsed.path
                        obj[key] = parsed._replace(netloc=host_header, path=new_path).geturl()
                for v in obj.values():
                    _rewrite(v)
            elif isinstance(obj, list):
                for v in obj:
                    _rewrite(v)
            return obj

        if upstream_path.startswith("/json/list") or upstream_path == "/json":
            pages = [t for t in data if isinstance(t, dict) and t.get("type") == "page"]
            if agent != _SHARED:
                pages = [t for t in pages if self.state.owner_of(t.get("id")) == agent]
            data = pages

        return 200, "application/json", json.dumps(_rewrite(data)).encode()

    # ── WS side: passive watcher (own) + per-connection proxy ─────────────

    async def run_watcher(self, stop_event: asyncio.Event) -> None:
        """Our own passive `Target.setDiscoverTargets` connection — same
        pattern as `browser_live.TargetWatcher`, kept independent of any
        agent connection's lifecycle."""
        while not stop_event.is_set():
            try:
                uri = f"ws://{self._upstream_host}:{self._upstream_port}/devtools/browser"
                version = await self._upstream_json("/json/version")
                ws_url = version.get("webSocketDebuggerUrl")
                if not ws_url:
                    raise RuntimeError("no webSocketDebuggerUrl")
                async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": True}}))
                    while not stop_event.is_set():
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        method = msg.get("method", "")
                        if method.startswith("Target.target"):
                            self.state.apply_target_event(method.split(".", 1)[1], msg.get("params", {}))
                        elif method == "Target.attachedToTarget":
                            self.state.observe_event(method, msg.get("params", {}))
            except Exception as e:
                logger.info("cdp_gateway watcher: %s, retrying", e)
                await asyncio.sleep(2.0)

    async def proxy_ws(self, client_ws, path: str, headers, peer_ip: Optional[str]) -> None:
        """Bidirectional proxy between one agent's WS connection and the
        real Chromium WS it names (browser-level or page-level). Reads every
        agent->Chromium request and every Chromium->agent response to keep
        `GatewayState` up to date — never rewrites or blocks a message yet
        (that is B3)."""
        agent = await self.identify(path, headers, peer_ip)
        upstream_path = strip_agent_prefix(path)
        upstream_uri = f"ws://{self._upstream_host}:{self._upstream_port}{upstream_path}"

        async with websockets.connect(upstream_uri, max_size=32 * 1024 * 1024) as upstream:
            pending_requests: dict[int, tuple[str, dict]] = {}

            async def from_client():
                async for raw in client_ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    method = msg.get("method")
                    params = msg.get("params", {}) or {}
                    if method and isinstance(msg.get("id"), int):
                        pending_requests[msg["id"]] = (method, params)
                    if "sessionId" in msg:
                        self.state.mark_active_by_session(msg["sessionId"])
                    await upstream.send(raw)

            async def from_upstream():
                async for raw in upstream:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        await client_ws.send(raw)
                        continue
                    if "id" in msg and msg["id"] in pending_requests:
                        method, params = pending_requests.pop(msg["id"])
                        self.state.observe_response(method, params, msg.get("result", {}) or {}, agent)
                    method = msg.get("method", "")
                    if method.startswith("Target.target"):
                        self.state.apply_target_event(method.split(".", 1)[1], msg.get("params", {}))
                        tid = (msg.get("params", {}).get("targetInfo") or {}).get("targetId") or msg.get("params", {}).get("targetId")
                        if method == "Target.targetCreated" and agent != _SHARED and tid:
                            # A target this agent's connection just created
                            # and did not get via an explicit createTarget
                            # response (e.g. window.open from page JS) still
                            # gets attributed, so the panel doesn't lose it.
                            if self.state.owner_of(tid) is None:
                                self.state.record_owner(tid, agent)
                    elif method == "Target.attachedToTarget":
                        self.state.observe_event(method, msg.get("params", {}))
                    await client_ws.send(raw)

            await asyncio.gather(from_client(), from_upstream())


def build_app(gateway: CdpGateway):
    """Returns (process_request, ws_handler) for `websockets.asyncio.server.serve`.

    A `/devtools/...` path (browser or page level) is a WS upgrade and falls
    through to `ws_handler`; every other path (`/json/version`, `/json/list`,
    `/mc/targets`, `/mc/health`, ...) is answered as plain HTTP here, same as
    Chromium's own debug port does today."""

    async def process_request(connection, request):
        path = request.path
        if "/devtools/" in path:
            return None  # let it upgrade to a WebSocket
        peer_ip = connection.remote_address[0] if connection.remote_address else None
        status, content_type, body = await gateway.handle_http(path, request.headers, peer_ip)
        response = connection.respond(status, body.decode("utf-8", "replace"))
        response.headers["Content-Type"] = content_type
        return response

    async def ws_handler(client_ws):
        request = client_ws.request
        path = request.path
        peer_ip = client_ws.remote_address[0] if client_ws.remote_address else None
        await gateway.proxy_ws(client_ws, path, request.headers, peer_ip)

    return process_request, ws_handler


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    gateway = CdpGateway()
    stop_event = asyncio.Event()
    watcher_task = asyncio.create_task(gateway.run_watcher(stop_event))
    process_request, ws_handler = build_app(gateway)
    async with ws_serve(ws_handler, "0.0.0.0", LISTEN_PORT, process_request=process_request):
        logger.info("cdp-gateway listening on :%d -> %s:%d", LISTEN_PORT, CHROMIUM_HOST, CHROMIUM_PORT)
        await asyncio.Future()
    stop_event.set()
    await watcher_task


if __name__ == "__main__":
    asyncio.run(main())
