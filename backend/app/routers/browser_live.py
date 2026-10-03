"""Live browser view — view-only CDP screencast of the agent browser.

The agent browser workflow runs against the shared `cdp-browser` service
(Chromium with an exposed CDP port, see docker-compose). playwright-mcp
drives it for the tester agents; this router attaches a SECOND, read-only
CDP session to the same pages and streams `Page.screencastFrame` JPEGs to
the operator UI over a WebSocket (pattern: cli_terminal WS proxy).

View-only by design: client messages are STEERING ONLY ({"follow": bool},
{"select": "<target id>"}) and never forwarded to Chromium. The only CDP
methods this module ever sends are: `Target.setDiscoverTargets` (on the
passive browser-level watcher connection) and `Page.startScreencast` /
`Page.screencastFrameAck` / `Page.stopScreencast` (on the per-page stream
connection). No input events, ever.
"""

import asyncio
import json
import logging
import os
import socket
import time
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from app.auth import require_user
from app.config import settings

logger = logging.getLogger("mc.browser_live")

router = APIRouter(prefix="/api/v1/browser-live", tags=["browser-live"])

# cdp-browser's in-container socat re-exposes Chromium's 127.0.0.1-only debug
# port on this host:port inside the docker network. Overridable for tests.
CDP_BASE_URL = os.environ.get("CDP_BROWSER_URL", "http://cdp-browser:9223")

# A newly-created tab (e.g. Playwright's "page for the next navigation") is
# briefly about:blank before the agent navigates it. Don't let it steal the
# "active" spot from a tab that is actually showing something, unless it is
# the only tab there is (live finding M11, bauplan.md).
_BLANK_URLS = {"about:blank", ""}

# Debounce window before the followed stream switches to a newly-active tab.
FOLLOW_SWITCH_DEBOUNCE_S = 0.6


def _resolve_cdp_netloc(base_url: str = None) -> str:
    """Chromium's debug endpoint rejects any Host header that is not an IP
    or localhost ("Host header is specified and is not an IP address or
    localhost", live finding 05.07.). Resolve the service name to its
    container IP per call — IPs change on container recreate."""
    parsed = urlparse(base_url or CDP_BASE_URL)
    host = parsed.hostname or "cdp-browser"
    port = parsed.port or 9223
    try:
        host = socket.gethostbyname(host)
    except OSError:
        pass  # tests / exotic setups: keep the name, let the call fail loudly
    return f"{host}:{port}"


def _rewrite_ws_url(ws_url: str, netloc: str) -> str:
    """CDP reports webSocketDebuggerUrl with its OWN idea of host (127.0.0.1)
    — rewrite host:port to the address we actually reach it under."""
    parsed = urlparse(ws_url)
    return parsed._replace(netloc=netloc).geturl()


async def _list_page_targets() -> list[dict]:
    netloc = _resolve_cdp_netloc()
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"http://{netloc}/json/list")
        resp.raise_for_status()
        targets = resp.json()
    # Chromium's /json/list already lists the NEWEST tab first (live-verified
    # 03.10. on cdp-browser: a tab opened via /json/new came before the older
    # about:blank). Keep that order — the UI defaults to the first entry, so
    # the panel opens on what the agent is working on right now. Until 03.10.
    # this list was reversed on the wrong assumption "newest last", and the
    # panel always opened the OLDEST tab (usually the idle about:blank).
    return [t for t in targets if t.get("type") == "page"]


@router.get("/targets")
async def list_targets(current_user=Depends(require_user)):
    """Open pages in the shared agent browser (for the live-view picker)."""
    try:
        pages = await _list_page_targets()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Agent-Browser (cdp-browser) nicht erreichbar: {e}",
        )
    return [
        {"id": t.get("id"), "title": t.get("title"), "url": t.get("url")}
        for t in pages
    ]


def _validate_ws_token(token: Optional[str]) -> bool:
    from app.auth import _query_jwt_subject

    # Legacy ?token= path only (ALLOW_QUERY_TOKEN_AUTH, no scoped tokens).
    return _query_jwt_subject(token) is not None


class TargetWatcher:
    """Tracks open pages in the shared agent browser and which one is
    "active" (most recently created or navigated), via ONE passive browser-
    level CDP connection (`Target.setDiscoverTargets`). Never attaches to a
    target, never sends input — purely observational (live finding M11).

    Falls back to polling `/json/list` every `poll_interval` seconds if the
    passive connection cannot be established or drops, so the panel keeps
    working (just without sub-2s updates) when the browser-level WS is
    unreachable for any reason.
    """

    def __init__(self, *, poll_interval: float = 2.0, now_fn=time.monotonic):
        self._targets: dict[str, dict] = {}
        self._poll_interval = poll_interval
        self._now = now_fn
        self.on_change = None  # Optional[Callable[[list[dict], Optional[str]], Any]]

    # ── event ingestion (used by both the live CDP path and tests) ────────

    def handle_event(self, method: str, params: dict) -> bool:
        """Apply one CDP `Target.*` event. Returns True if something in the
        tracked target set changed (callers decide whether to notify)."""
        now = self._now()
        if method == "targetCreated":
            info = params.get("targetInfo", {})
            if info.get("type") != "page":
                return False
            tid = info["targetId"]
            self._targets[tid] = {
                "id": tid,
                "title": info.get("title", ""),
                "url": info.get("url", ""),
                "created_at": now,
                "last_active_at": now,
            }
            return True
        if method == "targetInfoChanged":
            info = params.get("targetInfo", {})
            if info.get("type") != "page":
                return False
            tid = info["targetId"]
            existing = self._targets.get(tid)
            if existing is None:
                self._targets[tid] = {
                    "id": tid,
                    "title": info.get("title", ""),
                    "url": info.get("url", ""),
                    "created_at": now,
                    "last_active_at": now,
                }
                return True
            url_changed = existing["url"] != info.get("url", existing["url"])
            existing["title"] = info.get("title", existing["title"])
            existing["url"] = info.get("url", existing["url"])
            if url_changed:
                # Only an actual navigation counts as activity (M11) — a
                # pure title change (e.g. an SPA updating document.title)
                # must not steal "active" from a tab the agent is reading.
                existing["last_active_at"] = now
            return url_changed
        if method == "targetDestroyed":
            tid = params.get("targetId")
            if tid in self._targets:
                del self._targets[tid]
                return True
            return False
        return False

    def replace_from_list(self, pages: list[dict]) -> bool:
        """Used by the polling fallback: merge Chromium's /json/list into
        the tracked set, preserving known last_active_at / created_at and
        treating any brand-new id as freshly active."""
        now = self._now()
        changed = False
        seen = set()
        for t in pages:
            tid = t.get("id")
            if not tid:
                continue
            seen.add(tid)
            existing = self._targets.get(tid)
            if existing is None:
                self._targets[tid] = {
                    "id": tid,
                    "title": t.get("title", ""),
                    "url": t.get("url", ""),
                    "created_at": now,
                    "last_active_at": now,
                }
                changed = True
            else:
                url_changed = existing["url"] != t.get("url", existing["url"])
                existing["title"] = t.get("title", existing["title"])
                existing["url"] = t.get("url", existing["url"])
                if url_changed:
                    existing["last_active_at"] = now
                    changed = True
        for tid in list(self._targets):
            if tid not in seen:
                del self._targets[tid]
                changed = True
        return changed

    # ── derived state ───────────────────────────────────────────────────

    def targets(self) -> list[dict]:
        """Newest-active-first, for display."""
        return sorted(
            self._targets.values(), key=lambda t: t["last_active_at"], reverse=True
        )

    def active_id(self) -> Optional[str]:
        if not self._targets:
            return None
        non_blank = [
            t for t in self._targets.values() if t.get("url") not in _BLANK_URLS
        ]
        pool = non_blank or list(self._targets.values())
        return max(pool, key=lambda t: t["last_active_at"])["id"]

    def notify(self) -> None:
        if self.on_change is not None:
            self.on_change(self.targets(), self.active_id())


async def _watch_targets_browser_ws(watcher: TargetWatcher, stop_event: asyncio.Event) -> None:
    """Passive browser-level connection: `/json/version` → `webSocketDebuggerUrl`,
    send ONLY `Target.setDiscoverTargets {"discover": true}`, then read events
    forever. Raises/returns on any disconnect so the caller can fall back to
    polling."""
    import websockets

    netloc = _resolve_cdp_netloc()
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"http://{netloc}/json/version")
        resp.raise_for_status()
        browser_ws_url = _rewrite_ws_url(resp.json()["webSocketDebuggerUrl"], netloc)

    async with websockets.connect(browser_ws_url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Target.setDiscoverTargets",
            "params": {"discover": True},
        }))
        while not stop_event.is_set():
            raw = await ws.recv()
            msg = json.loads(raw)
            method = msg.get("method", "")
            if not method.startswith("Target.target"):
                continue
            short = method.split(".", 1)[1]  # targetCreated / targetInfoChanged / targetDestroyed
            if watcher.handle_event(short, msg.get("params", {})):
                watcher.notify()


async def _watch_targets_poll(watcher: TargetWatcher, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            pages = await _list_page_targets()
        except Exception as e:
            logger.info("browser_live poll fallback: cdp-browser unreachable: %s", e)
            pages = []
        if watcher.replace_from_list(pages):
            watcher.notify()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=watcher._poll_interval)
        except asyncio.TimeoutError:
            pass


async def run_target_watcher(watcher: TargetWatcher, stop_event: asyncio.Event) -> None:
    """Seed from /json/list, then prefer the passive WS; fall back to polling
    on any failure (M11's "ersatzweg"). Runs until stop_event is set."""
    try:
        watcher.replace_from_list(await _list_page_targets())
        watcher.notify()
    except Exception as e:
        logger.info("browser_live watcher: initial /json/list failed: %s", e)

    while not stop_event.is_set():
        try:
            await _watch_targets_browser_ws(watcher, stop_event)
        except Exception as e:
            if stop_event.is_set():
                return
            logger.info("browser_live watcher: browser WS unavailable (%s), polling instead", e)
            await _watch_targets_poll(watcher, stop_event)


@router.websocket("/ws")
async def browser_live_ws(
    websocket: WebSocket,
    token: Optional[str] = None,
    target: Optional[str] = None,
    ticket: Optional[str] = None,
    follow: int = 1,
):
    """Stream JPEG screencast frames of one agent-browser page to the client,
    optionally following whichever page is currently active.

    Auth: single-use stream ticket via ?ticket= (WebSocket can't send
    headers); legacy ?token=<jwt> only with ALLOW_QUERY_TOKEN_AUTH.
    ?target=<cdp target id> picks a starting page (default = active/newest).
    ?follow=1 (default) keeps switching to whichever page becomes active;
    a client {"select": "<id>"} message turns following off for that
    connection.

    Server → client messages:
      {"type": "frame", "data": "<base64 jpeg>", "metadata": {...}}
      {"type": "attached", "target": {"id", "title", "url"}}
      {"type": "targets", "targets": [...], "activeId": "...", "followedId": "..."}
      {"type": "status", "message": "..."}   (info/errors before close)

    Client → server messages are steering only, never forwarded to Chromium:
      {"follow": true|false}, {"select": "<target id>"}
    """
    from app.auth import authenticate_websocket

    if not await authenticate_websocket(websocket, token=token, ticket=ticket):
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()

    watcher = TargetWatcher()
    stop_event = asyncio.Event()
    state = {"followed_id": target, "following": bool(follow)}
    switch_requested = asyncio.Event()

    pending_update: dict = {}

    def on_targets_change(targets: list[dict], active_id: Optional[str]) -> None:
        # Called synchronously from the watcher task, same event loop —
        # no cross-thread handoff needed (TargetWatcher never runs in a
        # separate thread).
        pending_update["targets"] = targets
        pending_update["active_id"] = active_id
        switch_requested.set()

    watcher.on_change = on_targets_change
    watcher_task = asyncio.create_task(run_target_watcher(watcher, stop_event))

    async def send_targets_message() -> None:
        try:
            await websocket.send_json({
                "type": "targets",
                "targets": [
                    {"id": t["id"], "title": t["title"], "url": t["url"]}
                    for t in watcher.targets()
                ],
                "activeId": watcher.active_id(),
                "followedId": state["followed_id"],
            })
        except Exception:
            pass

    import websockets

    msg_id = 0

    def _next_id() -> int:
        nonlocal msg_id
        msg_id += 1
        return msg_id

    async def attach_and_stream(cdp_holder: dict) -> None:
        """(Re)attach the screencast to whichever page we should be showing,
        looping forever so a `select`/follow-switch can swap the underlying
        CDP connection without tearing down the client WebSocket."""
        while not stop_event.is_set():
            pages = watcher.targets() or await _list_page_targets()
            if not pages:
                await websocket.send_json({"type": "status", "message": "No open page in the agent browser yet."})
                await asyncio.sleep(1.0)
                continue

            wanted = state["followed_id"]
            page = next((p for p in pages if p.get("id") == wanted), None) or pages[0]
            state["followed_id"] = page["id"]

            # Resolve a fresh webSocketDebuggerUrl for this id (watcher
            # entries don't carry it; /json/list does).
            raw_pages = await _list_page_targets()
            raw = next((p for p in raw_pages if p.get("id") == page["id"]), None)
            if raw is None:
                await asyncio.sleep(0.5)
                continue
            ws_url = _rewrite_ws_url(raw["webSocketDebuggerUrl"], _resolve_cdp_netloc())

            try:
                async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as cdp:
                    cdp_holder["cdp"] = cdp
                    await cdp.send(json.dumps({
                        "id": _next_id(),
                        "method": "Page.startScreencast",
                        "params": {
                            "format": "jpeg",
                            "quality": 70,
                            "maxWidth": 1440,
                            "maxHeight": 900,
                            "everyNthFrame": 1,
                        },
                    }))
                    await websocket.send_json({
                        "type": "attached",
                        "target": {"id": raw.get("id"), "title": raw.get("title"), "url": raw.get("url")},
                    })

                    switch_requested.clear()
                    frame_task = asyncio.create_task(_pump(cdp))
                    switch_wait = asyncio.create_task(switch_requested.wait())
                    done, pending = await asyncio.wait(
                        {frame_task, switch_wait}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    try:
                        await cdp.send(json.dumps({"id": _next_id(), "method": "Page.stopScreencast"}))
                    except Exception:
                        pass

                    if switch_wait in done:
                        targets_now = pending_update.get("targets", watcher.targets())
                        active_now = pending_update.get("active_id", watcher.active_id())
                        await websocket.send_json({
                            "type": "targets",
                            "targets": [{"id": t["id"], "title": t["title"], "url": t["url"]} for t in targets_now],
                            "activeId": active_now,
                            "followedId": state["followed_id"],
                        })
                        if state["following"] and active_now and active_now != state["followed_id"]:
                            await asyncio.sleep(FOLLOW_SWITCH_DEBOUNCE_S)
                            state["followed_id"] = active_now
                        if frame_task in done:
                            frame_task.result()  # re-raise if the stream died
                        continue
                    frame_task.result()
                    return
            except WebSocketDisconnect:
                raise
            except Exception as e:
                logger.info("browser_live stream ended: %s", e)
                try:
                    await websocket.send_json({"type": "status", "message": str(e)[:200]})
                except Exception:
                    pass
                return

    async def _pump(cdp) -> None:
        while True:
            raw = await cdp.recv()
            msg = json.loads(raw)
            if msg.get("method") == "Page.screencastFrame":
                params = msg["params"]
                await cdp.send(json.dumps({
                    "id": _next_id(),
                    "method": "Page.screencastFrameAck",
                    "params": {"sessionId": params["sessionId"]},
                }))
                await websocket.send_json({
                    "type": "frame",
                    "data": params["data"],
                    "metadata": params.get("metadata", {}),
                })

    async def drain_client() -> None:
        # Steering messages only — NEVER forwarded to Chromium.
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            if "follow" in msg:
                state["following"] = bool(msg["follow"])
            if "select" in msg and isinstance(msg["select"], str):
                state["followed_id"] = msg["select"]
                state["following"] = False
                switch_requested.set()

    cdp_holder: dict = {}
    try:
        await send_targets_message()
        drain = asyncio.create_task(drain_client())
        stream = asyncio.create_task(attach_and_stream(cdp_holder))
        done, pending = await asyncio.wait(
            {drain, stream}, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception() if not t.cancelled() else None
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.info("browser_live ws ended: %s", e)
        try:
            await websocket.send_json({"type": "status", "message": str(e)[:200]})
        except Exception:
            pass
    finally:
        stop_event.set()
        watcher_task.cancel()
        try:
            await watcher_task
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
