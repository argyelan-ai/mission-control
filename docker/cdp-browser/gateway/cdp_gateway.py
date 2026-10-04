#!/usr/bin/env python3
"""cdp-gateway — agent-aware front door to the shared cdp-browser Chromium.

Runs inside the `cdp-browser` container next to Chromium and the existing
socat forwarder (port 9223, unchanged — the backend's screencast path and
playwright-mcp keep using it exactly as before). This gateway listens on
port 9300 and answers the same shapes Chromium's own debug port does
(`/json/version`, `/json/list`, `/json/new` incl. `PUT`, and the
`/devtools/browser|page/...` WebSockets) — but it also figures out WHICH
AGENT a connection belongs to and which open tab belongs to which agent, so
the operator's per-agent panel can show "Alpha's tabs" instead of "every
tab in the shared browser" (bauplan.md, PR B1).

Scope: identify + attribute only. It does **not** stop one agent from
seeing or touching another agent's tabs — that is PR B3's job, built on the
ownership map this module produces.

Identifying the agent behind a connection
-----------------------------------------
  1. URL path prefix `/a/<slug>/...`. This is THE mechanism. omp agents get
     it on every single request because their in-container relay
     (docker/omp-bridge/cdp_relay.py) prefixes each request line — omp's own
     `browser.cdpUrl` prefix is not enough on its own: Puppeteer's
     `connect({browserURL})` builds `new URL("/json/version", browserURL)`,
     an absolute path that DROPS any prefix (verified in the omp 18.1.10
     bundle, 04.10.2026). Playwright's connectOverCDP keeps the full path
     (bauplan M12), so a prefixed endpoint works there directly.
  2. Header `X-MC-Agent: <slug>`.
  3. Reverse DNS of the peer IP -> `mc-agent-<slug>.<network>` — LAST
     RESORT ONLY. Live on 04.10.2026 the PTR lookup for an agent container
     failed with "Name does not resolve" from inside cdp-browser, while the
     same lookup works in a freshly built network of the same shape; Docker's
     embedded DNS cannot be trusted to have (or keep) the record, and a stale
     record would name the wrong agent. Nothing depends on it any more.
  4. Otherwise `_shared` (unidentified — behaves exactly like Chromium).

A slug that doesn't match `_SLUG_RE` is treated as unidentified: this value
ends up in HTTP paths, WS URLs and `/mc/targets` JSON shown to the operator.

Attributing a tab to an agent
-----------------------------
  * `Target.createTarget` / `PUT /json/new` issued by an identified
    connection -> the new tab is that agent's.
  * A tab opened in an agent's browser context, or by `window.open()` from an
    agent's tab, inherits that owner.
  * **Claim on use** — an identified connection that sends `OMP.claimTarget`
    or a top-level navigation (`Page.navigate`, `Page.reload`,
    `Page.navigateToHistoryEntry`) for a tab makes that tab its own. This is
    what attributes omp's tabs at all: in "connected" mode omp NEVER creates
    a tab — it attaches to an existing page, sends `OMP.claimTarget` on a
    session for it (omp's own "this tab is mine now" marker; Chromium answers
    "method not found", which is forwarded untouched) and then
    `page.goto()`s it. The last agent to claim or navigate a tab owns it —
    that is the agent actually working in it.
  * Never from "this connection saw the event": every discover-enabled
    client receives `targetCreated` for every tab in the shared browser.

Direction (PRINCIPLES "build future-proof"): identity is by construction —
the `/a/<slug>` prefix every agent connection carries. That is exactly the key
a later "one browser context per agent" model (B3) maps to that agent's own
context; context-based attribution (`ctx_owner`) then covers every tab and
already exists here. Claim on use is the transitional rule for today's ONE
shared context, where omp reuses whatever page is open: it is isolated in
`observe_command` / `_CLAIM_METHODS`, and with per-agent contexts it can only
ever claim an agent's own tabs, so it needs no removal, just stops mattering.

Who stays unassigned: playwright-mcp (shared by every claude agent) talks to
:9223 directly, not through this gateway — and even through it, one MCP
server serving every agent could only ever be one identity. Its tabs show up
in `/mc/targets` with `agent: null`; the operator panel labels them "not
assigned to an agent" instead of pretending the agent has no tab.

Wire behaviour
--------------
One plain asyncio HTTP/1.1 front door (every method, so `PUT /json/new`
works — the previous `websockets`-server layer only parsed GET and left a
PUT hanging). HTTP answers always carry `Connection: close`. A WebSocket
upgrade on `/devtools/...` is passed to Chromium **byte for byte** (only the
request path loses its `/a/<slug>` prefix and the Host header becomes the
upstream IP, which Chromium insists on); `_FrameSniffer` reads a copy of
each direction's frames to keep `GatewayState` current and never alters,
delays or drops a byte. No message-size limit (the old proxy closed any
client message over 1 MiB with code 1009).

Trust model: identification is NOT authentication. Any container on the
Docker-internal network can already reach Chromium's CDP port directly
(:9223) — this gateway does not change that boundary, it only labels
connections for the UI. A container could claim to be any agent by sending
a matching path or header; an accepted limitation until B3's isolation + a
real per-agent credential exist.
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
from http import HTTPStatus
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import websockets

logger = logging.getLogger("cdp_gateway")

CHROMIUM_HOST = os.environ.get("CDP_GATEWAY_UPSTREAM_HOST", "127.0.0.1")
CHROMIUM_PORT = int(os.environ.get("CDP_GATEWAY_UPSTREAM_PORT", "9222"))
LISTEN_PORT = int(os.environ.get("CDP_GATEWAY_PORT", "9300"))

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHARED = "_shared"
_REVERSE_DNS_CACHE_SECONDS = 60.0
_BLANK_URLS = {"about:blank", ""}

# Request heads larger than this are not CDP traffic; the connection is
# refused instead of buffering without bound.
_MAX_HEAD_BYTES = 64 * 1024
# A CDP HTTP endpoint never takes a meaningful body; cap what we drain.
_MAX_BODY_BYTES = 1024 * 1024
_UPSTREAM_HTTP_TIMEOUT = 10.0
_PUMP_CHUNK = 64 * 1024
# After one side of a proxied WebSocket ends, the other direction gets this
# long to deliver what is already in flight before it is cut.
_HALF_CLOSE_GRACE = 0.5

# Commands that mean "the sending agent is working in this tab now".
_CLAIM_METHODS = frozenset({
    "OMP.claimTarget",
    "Page.navigate",
    "Page.reload",
    "Page.navigateToHistoryEntry",
})
# See GatewayState.reset_targets: owner/session maps outlive watcher resets
# and are pruned to live tabs only once they grow past this.
_OWNER_MAP_SOFT_CAP = 4096
# Only these requests need their response matched (ownership/session map).
_TRACKED_RESPONSE_METHODS = frozenset({
    "Target.createTarget",
    "Target.createBrowserContext",
    "Target.attachToTarget",
})


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


def page_id_from_path(path: str) -> Optional[str]:
    """`/devtools/page/<id>` -> `<id>` (the tab a page-level socket drives)."""
    prefix = "/devtools/page/"
    if path.startswith(prefix):
        tid = path[len(prefix):].split("?", 1)[0].strip("/")
        return tid or None
    return None


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
        # "mc-agent-alpha.<net>" -> "alpha" (M13).
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
    now_fn: Callable[[], float] = field(default=time.monotonic)

    # ── identification plumbing used by both HTTP and WS entry points ─────

    def owner_of(self, target_id: str) -> Optional[str]:
        return self.target_owner.get(target_id)

    def record_owner(self, target_id: str, agent: Optional[str]) -> None:
        if agent and agent != _SHARED:
            previous = self.target_owner.get(target_id)
            if previous and previous != agent:
                logger.info("cdp_gateway: tab %s moves from %s to %s", target_id, previous, agent)
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
            opener = info.get("openerId")
            # Attribution order: an owner already recorded against this id
            # (createTarget response, /json/new, a claim), then the browser
            # context's owner, then — for a `window.open()` from page JS,
            # which never goes through createTarget — the opener tab's own
            # owner. Never attribute from "whichever connection happened to
            # see this event": any connection with Target.setDiscoverTargets
            # on (every Puppeteer/omp client) receives targetCreated for
            # EVERY tab in the shared browser, not just its own (incident:
            # bauplan.md review, 03.10.2026).
            agent = (
                self.target_owner.get(tid)
                or (self.ctx_owner.get(ctx) if ctx else None)
                or (self.target_owner.get(opener) if opener else None)
            )
            existing = self.targets.get(tid)
            self.targets[tid] = TargetInfo(
                id=tid,
                title=info.get("title", ""),
                url=info.get("url", ""),
                created_at=existing.created_at if existing else now,
                last_active_at=existing.last_active_at if existing else now,
                agent=agent,
            )
            return existing is None
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
            self.target_owner.pop(tid, None)
            if tid in self.targets:
                del self.targets[tid]
                self._prune_owner_maps()
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

    def observe_command(
        self,
        method: Optional[str],
        session_id: Optional[str],
        agent: Optional[str],
        *,
        page_target: Optional[str] = None,
    ) -> None:
        """Feed one client -> Chromium command. Claim-on-use lives here (see
        the module docstring): `OMP.claimTarget` or a top-level navigation
        from an IDENTIFIED connection makes the tab it addresses the
        sender's. The tab is the session's target, or — on a page-level
        socket (`/devtools/page/<id>`), whose commands carry no sessionId —
        the page named in the URL."""
        if session_id:
            self.mark_active_by_session(session_id)
        if method not in _CLAIM_METHODS or not agent or agent == _SHARED:
            return
        tid = self.session_target.get(session_id) if session_id else page_target
        if tid:
            self.record_owner(tid, agent)

    def observe_response(self, request_method: str, params: dict, result: dict, agent: Optional[str]) -> None:
        """Feed the result of a Target.* command, so later traffic from/about
        the resulting session, context or target can be attributed. The
        session -> target map is a plain fact about the browser and is kept
        for every connection; ownership only for an identified agent."""
        if request_method == "Target.attachToTarget":
            session_id = result.get("sessionId")
            tid = params.get("targetId")
            if session_id and tid:
                self.session_target[session_id] = tid
            return
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

    def observe_event(self, method: str, params: dict) -> None:
        if method == "Target.attachedToTarget":
            session_id = params.get("sessionId")
            tid = (params.get("targetInfo") or {}).get("targetId")
            if session_id and tid:
                self.session_target[session_id] = tid
        elif method == "Target.detachedFromTarget":
            session_id = params.get("sessionId")
            if session_id:
                self.session_target.pop(session_id, None)

    # ── read side for /mc/targets and per-agent /json/list ─────────────────

    def reset_targets(self) -> None:
        """Called on every watcher (re)connect: `Target.setDiscoverTargets`
        re-emits a fresh `targetCreated` for every tab that still exists, but
        it never emits `targetDestroyed` for one that closed WHILE the
        watcher was disconnected — without a reset, a tab that closed during
        an outage lingers in `targets` forever (review finding: stale entries
        can linger across a reconnect).

        Ownership (`target_owner`, `ctx_owner`) and the session map are NOT
        cleared: they are keyed by Chromium's target/context/session GUIDs,
        which are never reused, so an entry can't attach to a different tab;
        and the agent connections they came from are unaffected by OUR
        watcher reconnecting. Clearing them made every tab an agent had
        claimed fall back to "unassigned" after a mere watcher hiccup, until
        the agent happened to navigate again. Only live targets are ever
        reported, so a dead tab's leftover owner entry is invisible; the map
        is pruned to live targets once it grows past `_OWNER_MAP_SOFT_CAP`."""
        self.targets.clear()

    def _prune_owner_maps(self) -> None:
        if len(self.target_owner) > _OWNER_MAP_SOFT_CAP and self.targets:
            self.target_owner = {k: v for k, v in self.target_owner.items() if k in self.targets}
        if len(self.session_target) > _OWNER_MAP_SOFT_CAP and self.targets:
            self.session_target = {k: v for k, v in self.session_target.items() if v in self.targets}

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
                # None = not assigned to any agent (playwright-mcp's tabs, a
                # tab no identified agent has created/claimed/navigated yet).
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


# ── HTTP/1.1 plumbing (stdlib only) ────────────────────────────────────────


class Headers:
    """Ordered, case-insensitive header list (enough for CDP's HTTP side)."""

    def __init__(self, items: Optional[list[tuple[str, str]]] = None):
        self.items: list[tuple[str, str]] = list(items or [])

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        lname = name.lower()
        for k, v in self.items:
            if k.lower() == lname:
                return v
        return default


@dataclass
class HttpRequest:
    method: str
    target: str  # origin-form path incl. query, as sent
    version: str
    headers: Headers

    @property
    def is_websocket_upgrade(self) -> bool:
        return (self.headers.get("Upgrade") or "").lower() == "websocket"


def parse_request_head(blob: bytes) -> Optional[HttpRequest]:
    try:
        text = blob.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes anything
        return None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return None
    method, target, version = parts
    if not target.startswith("/"):
        return None
    items: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            return None
        items.append((name.strip(), value.strip()))
    return HttpRequest(method=method.upper(), target=target, version=version, headers=Headers(items))


async def _read_head(reader: asyncio.StreamReader) -> Optional[bytes]:
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        return None


def _status_line(status: int) -> str:
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "Unknown"
    return f"HTTP/1.1 {status} {reason}\r\n"


async def _respond(
    writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes, *, head_only: bool = False,
) -> None:
    head = (
        _status_line(status)
        + f"Content-Type: {content_type}\r\n"
        + f"Content-Length: {len(body)}\r\n"
        + "Connection: close\r\n\r\n"
    )
    writer.write(head.encode("latin-1") + (b"" if head_only else body))
    await writer.drain()


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


# ── passive WebSocket frame reader ─────────────────────────────────────────


def _unmask(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    if not n:
        return payload
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


class _FrameSniffer:
    """Reads a COPY of one direction's WebSocket byte stream and hands every
    complete JSON text message to `on_message`. The caller forwards the
    bytes itself, unchanged — this class can only observe.

    Handles masked (client -> server) and unmasked frames, fragmented
    messages, interleaved control frames and any split across `feed()`
    calls. Messages larger than `max_message` (screenshots, DOM dumps — no
    Target.* message is ever that big) are skipped without buffering. Any
    parse surprise (e.g. a compressed frame) switches the sniffer off for
    the rest of the connection: attribution degrades, traffic never does."""

    def __init__(self, on_message: Callable[[dict], None], *, max_message: int = 1024 * 1024):
        self._on_message = on_message
        self._max = max_message
        self._buf = bytearray()
        self._skip = 0
        self._frag: Optional[bytearray] = None
        self._frag_opcode = 0
        self._dropping_fragments = False
        self.broken = False

    def feed(self, data: bytes) -> None:
        if self.broken or not data:
            return
        try:
            self._feed(data)
        except Exception as e:  # noqa: BLE001 - observation must never break the proxy
            logger.info("cdp_gateway: frame sniffer off for this connection: %s", e)
            self.broken = True
            self._buf.clear()

    def _feed(self, data: bytes) -> None:
        self._buf += data
        while self._buf:
            if self._skip:
                n = min(self._skip, len(self._buf))
                del self._buf[:n]
                self._skip -= n
                continue
            header = self._parse_header()
            if header is None:
                return
            fin, opcode, mask, header_len, length = header
            if length > self._max:
                del self._buf[:header_len]
                self._skip = length
                if opcode < 0x8:
                    # This data frame's message is lost to sniffing; drop the
                    # rest of it too if it continues in later frames.
                    self._frag = None
                    self._dropping_fragments = not fin
                continue
            if len(self._buf) < header_len + length:
                return
            payload = bytes(self._buf[header_len:header_len + length])
            del self._buf[:header_len + length]
            if mask:
                payload = _unmask(payload, mask)
            self._on_frame(fin, opcode, payload)

    def _parse_header(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        if b0 & 0x70:
            raise ValueError("RSV bits set (compressed or extended frame)")
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        pos = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = int.from_bytes(buf[2:4], "big")
            pos = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = int.from_bytes(buf[2:10], "big")
            pos = 10
        mask = b""
        if masked:
            if len(buf) < pos + 4:
                return None
            mask = bytes(buf[pos:pos + 4])
            pos += 4
        return fin, opcode, mask, pos, length

    def _on_frame(self, fin: bool, opcode: int, payload: bytes) -> None:
        if opcode >= 0x8:
            return  # close/ping/pong — may sit between fragments
        if opcode in (0x1, 0x2):
            self._dropping_fragments = False
            if fin:
                if opcode == 0x1:
                    self._deliver(payload)
                self._frag = None
                return
            self._frag = bytearray(payload)
            self._frag_opcode = opcode
            return
        # opcode 0x0: continuation
        if self._dropping_fragments:
            if fin:
                self._dropping_fragments = False
            return
        if self._frag is None:
            return
        self._frag += payload
        if len(self._frag) > self._max:
            self._frag = None
            self._dropping_fragments = not fin
            return
        if fin:
            message, op = bytes(self._frag), self._frag_opcode
            self._frag = None
            if op == 0x1:
                self._deliver(message)

    def _deliver(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        if isinstance(msg, dict):
            self._on_message(msg)


class _ConnectionObserver:
    """Per-connection CDP bookkeeping fed by the two sniffers."""

    def __init__(self, state: GatewayState, agent: str, page_target: Optional[str]):
        self._state = state
        self._agent = agent
        self._page_target = page_target
        self._pending: dict[tuple[Optional[str], int], tuple[str, dict]] = {}
        # Flattened CDP sessions live and die with the connection that
        # opened them; their ids are dropped from the shared map on close.
        self._sessions: set[str] = set()

    def close(self) -> None:
        for session_id in self._sessions:
            self._state.session_target.pop(session_id, None)
        self._sessions.clear()

    def from_client(self, msg: dict) -> None:
        method = msg.get("method")
        session_id = msg.get("sessionId")
        if isinstance(method, str) and isinstance(msg.get("id"), int) and method in _TRACKED_RESPONSE_METHODS:
            self._pending[(session_id, msg["id"])] = (method, msg.get("params") or {})
        self._state.observe_command(
            method if isinstance(method, str) else None,
            session_id if isinstance(session_id, str) else None,
            self._agent,
            page_target=self._page_target,
        )

    def from_upstream(self, msg: dict) -> None:
        msg_id = msg.get("id")
        if isinstance(msg_id, int):
            pending = self._pending.pop((msg.get("sessionId"), msg_id), None)
            if pending is not None:
                method, params = pending
                result = msg.get("result") or {}
                self._state.observe_response(method, params, result, self._agent)
                if method == "Target.attachToTarget" and isinstance(result.get("sessionId"), str):
                    self._sessions.add(result["sessionId"])
            return
        method = msg.get("method") or ""
        if method.startswith("Target.target"):
            # Attribution for the resulting target comes only from
            # GatewayState itself (recorded owner / context / opener) —
            # never from "this connection happened to observe the event".
            self._state.apply_target_event(method.split(".", 1)[1], msg.get("params") or {})
        elif method in ("Target.attachedToTarget", "Target.detachedFromTarget"):
            params = msg.get("params") or {}
            self._state.observe_event(method, params)
            if method == "Target.attachedToTarget" and isinstance(params.get("sessionId"), str):
                self._sessions.add(params["sessionId"])


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, sniffer: Optional[_FrameSniffer]) -> None:
    while True:
        data = await reader.read(_PUMP_CHUNK)
        if not data:
            return
        if sniffer is not None:
            sniffer.feed(data)
        writer.write(data)
        await writer.drain()


class CdpGateway:
    def __init__(self, *, upstream_host: str = CHROMIUM_HOST, upstream_port: int = CHROMIUM_PORT):
        self.state = GatewayState()
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._dns_cache = _ReverseDnsCache()
        # bauplan.md says /mc/health means "the watcher connection is up", not
        # just "this process is alive" — a dead watcher silently stops
        # tracking every tab (no more ownership, no more active/newest),
        # which is exactly the kind of failure a healthcheck exists to catch
        # (review finding, 03.10.2026: /mc/health always said 200 even then).
        self._watcher_connected = False

    @property
    def _upstream_netloc(self) -> str:
        return f"{self._upstream_host}:{self._upstream_port}"

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

    # ── upstream (Chromium) HTTP ───────────────────────────────────────────

    async def _upstream_http(self, method: str, path: str) -> tuple[int, str, bytes]:
        """One request to Chromium's debug port -> (status, content type,
        body). Reads exactly `Content-Length` bytes rather than "until EOF":
        Chromium's debug HTTP server keeps the connection alive regardless
        of `Connection: close` (live-verified against a real Chromium 124 —
        a bound-less read simply hung forever)."""

        async def _do() -> tuple[int, str, bytes]:
            reader, writer = await asyncio.open_connection(self._upstream_host, self._upstream_port)
            try:
                # Chromium's debug HTTP server derives the host:port it
                # reports back in `webSocketDebuggerUrl` from the Host header
                # of the VERY REQUEST asking for it (live-verified against a
                # real Chromium 124: a bare "Host: 127.0.0.1" with no port got
                # back "ws://127.0.0.1/devtools/..." — no port at all, which
                # then made the watcher's connect default to port 80 and fail
                # forever). Always send the port.
                body_header = "Content-Length: 0\r\n" if method in ("PUT", "POST") else ""
                req = (
                    f"{method} {path} HTTP/1.1\r\n"
                    f"Host: {self._upstream_netloc}\r\n"
                    f"{body_header}"
                    "Connection: close\r\n\r\n"
                )
                writer.write(req.encode("latin-1"))
                await writer.drain()
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                status = int(lines[0].split(" ", 2)[1])
                content_length: Optional[int] = None
                content_type = "application/octet-stream"
                for line in lines[1:]:
                    name, _, value = line.partition(":")
                    lname = name.strip().lower()
                    if lname == "content-length":
                        content_length = int(value.strip())
                    elif lname == "content-type":
                        content_type = value.strip()
                if method == "HEAD" or status in (204, 304) or 100 <= status < 200:
                    # No body by definition, whatever Content-Length says —
                    # reading one would wait for bytes that never come
                    # (review finding: HEAD hung 10 s, then 502).
                    body = b""
                elif content_length is not None:
                    body = await reader.readexactly(content_length) if content_length else b""
                else:
                    body = await reader.read()
                return status, content_type, body
            finally:
                await _close_writer(writer)

        return await asyncio.wait_for(_do(), timeout=_UPSTREAM_HTTP_TIMEOUT)

    async def _upstream_json(self, path: str) -> dict:
        """GET a JSON endpoint off Chromium's debug port."""
        _status, _ctype, body = await self._upstream_http("GET", path)
        return json.loads(body or b"{}")

    # ── HTTP side: /json/*, /mc/targets, /mc/health ────────────────────────

    async def handle_http(self, path: str, headers, peer_ip: Optional[str], method: str = "GET"):
        """Returns (status, content_type, body_bytes) for a plain HTTP
        request (any method — `PUT /json/new` included)."""
        local_path = strip_agent_prefix(path)

        # MC's own endpoints first: the backend polls /mc/targets every
        # ~1.5 s per open panel, and none of this needs to know who asks.
        if local_path == "/mc/health":
            if self._watcher_connected:
                return 200, "text/plain", b"ok"
            return 503, "text/plain", b"cdp-gateway: watcher not connected"
        if local_path.startswith("/mc/targets"):
            want = None
            if "?" in local_path:
                qs = parse_qs(local_path.split("?", 1)[1])
                raw = (qs.get("agent") or [None])[0]
                want = normalize_slug(raw) if raw else None
            body = json.dumps(self.state.as_mc_targets_json(want)).encode()
            return 200, "application/json", body

        agent = await self.identify(path, headers, peer_ip)
        try:
            status, content_type, body = await self._upstream_http(method, local_path)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as e:
            return 502, "text/plain", f"cdp-gateway: upstream unreachable: {e}".encode()
        if status != 200 or "json" not in content_type.lower():
            return status, content_type, body
        try:
            data = json.loads(body or b"null")
        except ValueError:
            return status, content_type, body

        host_header = headers.get("Host") or f"127.0.0.1:{LISTEN_PORT}"
        prefix = f"/a/{agent}" if agent != _SHARED else ""

        def _rewrite(obj):
            if isinstance(obj, dict):
                ws_url = obj.get("webSocketDebuggerUrl")
                if ws_url:
                    parsed = urlparse(ws_url)
                    obj["webSocketDebuggerUrl"] = parsed._replace(
                        netloc=host_header, path=prefix + parsed.path,
                    ).geturl()
                for v in obj.values():
                    _rewrite(v)
            elif isinstance(obj, list):
                for v in obj:
                    _rewrite(v)
            return obj

        route = local_path.split("?", 1)[0]
        if route in ("/json/list", "/json", "/json/list/", "/json/"):
            # An identified agent's /json/list shows only its own tabs. omp
            # does not depend on it (checked in the 18.1.10 bundle: it picks
            # a page from Puppeteer's CDP target list — `browser.targets()` /
            # `browser.pages()`, first visible one — and only SERVES /json/list
            # in its own browser relay), so a fresh agent with no tab of its
            # own still reuses and claims an open page, no tab growth (seen in
            # the live-identical replica: each agent's first run reused the
            # one open tab).
            pages = [t for t in data if isinstance(t, dict) and t.get("type") == "page"]
            if agent != _SHARED:
                pages = [t for t in pages if self.state.owner_of(t.get("id")) == agent]
            data = pages
        elif route.rstrip("/") == "/json/new" and isinstance(data, dict) and data.get("id"):
            # A tab opened over HTTP belongs to whoever opened it — the
            # watcher's targetCreated for it may arrive before or after this;
            # `apply_target_event` picks the recorded owner up either way.
            self.state.record_owner(data["id"], agent)

        return 200, "application/json", json.dumps(_rewrite(data)).encode()

    # ── connection handling ────────────────────────────────────────────────

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else None
        try:
            head = await _read_head(reader)
            if head is None:
                return
            req = parse_request_head(head)
            if req is None:
                await _respond(writer, 400, "text/plain", b"cdp-gateway: malformed request")
                return
            if req.is_websocket_upgrade and "/devtools/" in req.target:
                await self.proxy_ws(reader, writer, req, peer_ip)
                return
            length = req.headers.get("Content-Length")
            if length:
                try:
                    n = int(length)
                except ValueError:
                    n = -1
                if n < 0 or n > _MAX_BODY_BYTES:
                    await _respond(writer, 413, "text/plain", b"cdp-gateway: body not accepted")
                    return
                if n:
                    await reader.readexactly(n)
            status, content_type, body = await self.handle_http(req.target, req.headers, peer_ip, req.method)
            await _respond(writer, status, content_type, body, head_only=req.method == "HEAD")
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as e:  # noqa: BLE001 - one bad connection must never take the server down
            logger.info("cdp_gateway: connection error: %s", e)
        finally:
            await _close_writer(writer)

    # ── WS side: passive watcher (own) + per-connection proxy ─────────────

    async def run_watcher(self, stop_event: asyncio.Event) -> None:
        """Our own passive `Target.setDiscoverTargets` connection — same
        pattern as `browser_live.TargetWatcher`, kept independent of any
        agent connection's lifecycle."""
        while not stop_event.is_set():
            try:
                version = await self._upstream_json("/json/version")
                ws_url = version.get("webSocketDebuggerUrl")
                if not ws_url:
                    raise RuntimeError("no webSocketDebuggerUrl")
                async with websockets.connect(ws_url, max_size=None) as ws:
                    # A tab that closed WHILE we were disconnected never
                    # sends us its targetDestroyed — start this (re)connect
                    # from a clean slate so discover's fresh targetCreated
                    # burst is the only truth, not a merge with stale state.
                    self.state.reset_targets()
                    await ws.send(json.dumps({"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": True}}))
                    self._watcher_connected = True
                    while not stop_event.is_set():
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        method = msg.get("method", "")
                        if method.startswith("Target.target"):
                            self.state.apply_target_event(method.split(".", 1)[1], msg.get("params", {}))
                        elif method in ("Target.attachedToTarget", "Target.detachedFromTarget"):
                            self.state.observe_event(method, msg.get("params", {}))
            except Exception as e:
                logger.info("cdp_gateway watcher: %s, retrying", e)
                self._watcher_connected = False
                await asyncio.sleep(2.0)
            finally:
                self._watcher_connected = False

    async def proxy_ws(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        req: HttpRequest,
        peer_ip: Optional[str],
    ) -> None:
        """Pass one agent's CDP WebSocket through to Chromium unchanged,
        reading a copy of both directions to keep `GatewayState` current."""
        agent = await self.identify(req.target, req.headers, peer_ip)
        upstream_path = strip_agent_prefix(req.target)
        try:
            up_reader, up_writer = await asyncio.open_connection(self._upstream_host, self._upstream_port)
        except OSError as e:
            await _respond(client_writer, 502, "text/plain", f"cdp-gateway: upstream unreachable: {e}".encode())
            return
        try:
            lines = [f"GET {upstream_path} HTTP/1.1", f"Host: {self._upstream_netloc}"]
            for name, value in req.headers.items:
                lname = name.lower()
                # Host: Chromium only accepts an IP/localhost. Extensions:
                # never let permessage-deflate get negotiated, or the sniffer
                # would see compressed frames (it then switches itself off —
                # attribution lost, traffic fine). X-MC-Agent: ours, not
                # Chromium's.
                if lname in ("host", "sec-websocket-extensions", "x-mc-agent"):
                    continue
                lines.append(f"{name}: {value}")
            up_writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            await up_writer.drain()

            resp_head = await _read_head(up_reader)
            if resp_head is None:
                await _respond(client_writer, 502, "text/plain", b"cdp-gateway: no handshake answer from Chromium")
                return
            client_writer.write(resp_head)
            await client_writer.drain()
            try:
                upgraded = int(resp_head.split(b" ", 2)[1]) == 101
            except (IndexError, ValueError):
                upgraded = False

            observer = _ConnectionObserver(self.state, agent, page_id_from_path(upstream_path))
            c2u = _FrameSniffer(observer.from_client) if upgraded else None
            u2c = _FrameSniffer(observer.from_upstream) if upgraded else None
            t_client = asyncio.ensure_future(_pump(client_reader, up_writer, c2u))
            t_upstream = asyncio.ensure_future(_pump(up_reader, client_writer, u2c))
            # FIRST_COMPLETED, not gather: when either side goes away the
            # other must be torn down promptly (review finding 03.10.2026: a
            # dangling task per disconnected agent held a live browser-level
            # CDP session open and hung server shutdown). The side whose
            # source ended is half-closed and the other direction gets a
            # short grace to deliver what is already in flight (a close
            # frame, a last response), like socat's -t.
            try:
                done, pending = await asyncio.wait({t_client, t_upstream}, return_when=asyncio.FIRST_COMPLETED)
                for task, writer in ((t_client, up_writer), (t_upstream, client_writer)):
                    if task in done:
                        try:
                            if writer.can_write_eof():
                                writer.write_eof()
                        except (OSError, RuntimeError):
                            pass
                if pending:
                    await asyncio.wait(pending, timeout=_HALF_CLOSE_GRACE)
            finally:
                for task in (t_client, t_upstream):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(t_client, t_upstream, return_exceptions=True)
                observer.close()
        finally:
            await _close_writer(up_writer)


async def start_server(gateway: CdpGateway, host: str = "0.0.0.0", port: int = LISTEN_PORT):
    """Starts the gateway's listening socket; returns the asyncio Server."""
    return await asyncio.start_server(gateway.handle_connection, host, port, limit=_MAX_HEAD_BYTES)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    gateway = CdpGateway()
    stop_event = asyncio.Event()
    watcher_task = asyncio.create_task(gateway.run_watcher(stop_event))
    server = await start_server(gateway)
    logger.info("cdp-gateway listening on :%d -> %s:%d", LISTEN_PORT, CHROMIUM_HOST, CHROMIUM_PORT)
    try:
        async with server:
            await server.serve_forever()
    finally:
        stop_event.set()
        watcher_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
