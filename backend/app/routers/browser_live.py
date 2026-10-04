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
import uuid
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from app.auth import require_user
from app.config import settings
from app.database import async_session_maker
from app.models.agent import Agent

logger = logging.getLogger("mc.browser_live")

router = APIRouter(prefix="/api/v1/browser-live", tags=["browser-live"])

# cdp-browser's in-container socat re-exposes Chromium's 127.0.0.1-only debug
# port on this host:port inside the docker network. Overridable for tests.
CDP_BASE_URL = os.environ.get("CDP_BROWSER_URL", "http://cdp-browser:9223")

# cdp-gateway (bauplan.md PR B1) — same container as CDP_BASE_URL, different
# port. Answers `GET /mc/targets[?agent=<slug>]`: which open pages belong to
# which agent (`agent: null` = not assigned to any agent), figured out from
# the agent's own CDP connection — see docker/cdp-browser/gateway/cdp_gateway.py
# (path prefix the omp relay adds; tabs claimed on use). Used ONLY to FILTER the
# `targets`/`active_id` picture this module already builds from Chromium
# directly — the screencast itself (attach_and_stream) is unchanged and
# still a read-only second CDP session straight to Chromium, same as before
# B1. If the gateway is unreachable, every call here degrades to "no
# attribution" (None), and callers fall back to showing every tab — a
# gateway outage must never make the whole live-view panel unusable.
GATEWAY_BASE_URL = os.environ.get("CDP_GATEWAY_URL", "http://cdp-browser:9300")

# A newly-created tab (e.g. Playwright's "page for the next navigation") is
# briefly about:blank before the agent navigates it. Don't let it steal the
# "active" spot from a tab that is actually showing something, unless it is
# the only tab there is (live finding M11, bauplan.md).
_BLANK_URLS = {"about:blank", ""}


ALLOWED_CDP_METHODS = {
    "Target.setDiscoverTargets",
    "Page.startScreencast",
    "Page.screencastFrameAck",
    "Page.stopScreencast",
}


async def _send_cdp(conn, msg_id: int, method: str, params: Optional[dict] = None) -> None:
    """Single choke point for everything this module ever sends to a CDP
    socket (browser-level watcher OR per-page stream). Runtime-asserts the
    method against the view-only allow-list, so a future call-site that
    forgets this invariant fails loudly instead of silently widening what
    the "view-only" guarantee covers (finding: the static regex probe only
    catches literal strings, not one built from a variable/f-string)."""
    assert method in ALLOWED_CDP_METHODS, f"browser_live: refusing non-view-only CDP method {method!r}"
    payload: dict = {"id": msg_id, "method": method}
    if params is not None:
        payload["params"] = params
    await conn.send(json.dumps(payload))


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


async def _agent_slug(agent_id: str) -> Optional[str]:
    """Same derivation `agent_lifecycle`/`agent_bootstrap` already use for
    the container/workspace name: the persisted `slug` column, falling back
    to a name-derived one for older rows that predate it. Returns None for
    an unknown id (caller then shows every tab, same as not passing
    `agent_id` at all — an agent the operator can't resolve must never hide
    the whole panel).

    Opens its own short-lived session via `async_session_maker()` rather
    than taking `Depends(get_session)` on the route: `get_session` needs a
    `Request` to key its managed_session timing/logging, which a WebSocket
    route never has, and existing tests mount this router on a minimal app
    with no `get_session` override at all (test_browser_live_ws_handler.py)."""
    try:
        agent_uuid = uuid.UUID(agent_id)
    except (ValueError, AttributeError, TypeError):
        return None
    try:
        async with async_session_maker() as session:
            agent = await session.get(Agent, agent_uuid)
    except Exception as e:
        logger.info("browser_live: could not resolve agent %s: %s", agent_id, e)
        return None
    if agent is None:
        return None
    return agent.slug or (agent.name or "").lower().replace(" ", "-") or None


_gateway_last_reachable: dict[str, bool] = {}


async def _gateway_owned_ids(agent_slug: str) -> Optional[set[str]]:
    """Target ids `cdp-gateway` currently attributes to `agent_slug`, or
    None if the gateway can't be reached (NOT the same as "empty set" —
    an empty set is a real, meaningful "this agent has no tabs open" that
    the UI shows as its own empty state; None means "attribution isn't
    available right now, fall back to showing everything" per bauplan.md's
    graceful-degradation rule).

    Every open scoped panel calls this roughly once per cache TTL (~1.5s),
    so during a real gateway outage this fires constantly — logged only on
    the up→down and down→up transitions (keyed per agent_slug), not on
    every call, so a dead gateway doesn't flood the log for as long as a
    panel stays open (low finding)."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{GATEWAY_BASE_URL}/mc/targets", params={"agent": agent_slug})
            resp.raise_for_status()
            rows = resp.json()
    except Exception as e:
        if _gateway_last_reachable.get(agent_slug, True):
            logger.info("browser_live: cdp-gateway unreachable for agent filter: %s", e)
            _gateway_last_reachable[agent_slug] = False
        return None
    if not _gateway_last_reachable.get(agent_slug, True):
        logger.info("browser_live: cdp-gateway reachable again for agent filter %s", agent_slug)
    _gateway_last_reachable[agent_slug] = True
    return {row["targetId"] for row in rows if row.get("targetId")}


async def _gateway_assigned_ids() -> Optional[set[str]]:
    """Target ids cdp-gateway attributes to ANY agent, or None if it can't
    be reached. A live page that is not in this set is "not assigned to an
    agent" — playwright-mcp's tabs (shared by every claude agent, never
    attributed), or a tab no identified agent has claimed yet. Reachability
    logging stays with `_gateway_owned_ids`, which every scoped call makes
    first anyway."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{GATEWAY_BASE_URL}/mc/targets")
            resp.raise_for_status()
            rows = resp.json()
    except Exception:
        return None
    return {row["targetId"] for row in rows if row.get("targetId") and row.get("agent")}


def _apply_scope(
    pages: list[dict], owned: Optional[set[str]], assigned: Optional[set[str]],
) -> tuple[list[dict], Optional[set[str]]]:
    """The scoping rule for an agent's panel -> (pages to show, ids of the
    unassigned pages when the unassigned fallback is on, else None).

    - attribution unavailable (`owned` None): every page (scope_unavailable).
    - the agent owns at least one open page: only those.
    - it owns none, but some open page is NOT assigned to any agent: every
      page, with the unassigned ones flagged. Those could be this agent's
      (live 04.10.2026: every tab was unassigned and the panel claimed the
      agent had no tab while it had one open) — so never say "no open tab"
      then; show them and say they aren't assigned.
    - every open page belongs to some OTHER agent: nothing — a real "this
      agent has no open tab".
    - assignment unknown (`assigned` None, gateway flapped between the two
      calls): nothing, no fallback claimed; the next poll decides."""
    if owned is None:
        return pages, None
    own = [p for p in pages if p.get("id") in owned]
    if own or assigned is None:
        return own, None
    unassigned = {p.get("id") for p in pages if p.get("id") not in assigned}
    if unassigned:
        return pages, unassigned
    return [], None


@router.get("/targets")
async def list_targets(
    agent_id: Optional[str] = None,
    current_user=Depends(require_user),
):
    """Open pages in the shared agent browser (for the live-view picker).

    `agent_id` (bauplan.md PR B1): when given and `cdp-gateway` is reachable
    and knows this agent, the list is filtered to that agent's own tabs.
    Omitted, unresolvable, or gateway-down → every tab in the shared
    browser, exactly as before B1 (never a harder failure than that) — but
    `scopeUnavailable: true` tells the caller the list is NOT actually
    scoped, so the UI can say so instead of silently looking like a
    (misleadingly empty-looking "only agent X") filtered view.
    `unassignedFallback: true` — the agent owns no open tab but tabs that
    are assigned to no agent exist: every tab is listed, the unassigned ones
    carry `unassigned: true` (see `_apply_scope`)."""
    try:
        pages = await _list_page_targets()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Agent-Browser (cdp-browser) nicht erreichbar: {e}",
        )
    scope_unavailable = False
    unassigned: Optional[set[str]] = None
    if agent_id:
        slug = await _agent_slug(agent_id)
        if slug:
            owned_ids = await _gateway_owned_ids(slug)
            scope_unavailable = owned_ids is None
            assigned: Optional[set[str]] = None
            if owned_ids is not None and not any(p.get("id") in owned_ids for p in pages):
                assigned = await _gateway_assigned_ids()
            pages, unassigned = _apply_scope(pages, owned_ids, assigned)
        else:
            scope_unavailable = True
    targets = []
    for t in pages:
        row = {"id": t.get("id"), "title": t.get("title"), "url": t.get("url")}
        if unassigned is not None and t.get("id") in unassigned:
            row["unassigned"] = True
        targets.append(row)
    return {
        "targets": targets,
        "scopeUnavailable": scope_unavailable,
        # Scoped, the agent owns no open tab, but unassigned tabs exist — so
        # `targets` is EVERY tab (unassigned ones flagged), not "this agent
        # has none". See `_apply_scope`.
        "unassignedFallback": unassigned is not None,
        "unassignedCount": len(unassigned) if unassigned else 0,
    }


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
            # NOTE (finding, round 4): the finding's *optional* suggestion —
            # also returning True for a title-only change, so the picker's
            # label follows `document.title` without a navigation — was
            # tried and reverted: it breaks the existing, deliberate
            # contract covered by
            # `test_watcher_pure_title_change_does_not_move_active` and
            # `test_watcher_on_change_callback_fires_only_on_real_change`
            # (test_browser_live.py), which assert a pure title update is
            # NOT a "change" worth a watcher notification. The REQUIRED part
            # of this finding — url/title in `send_targets_message`'s own
            # signature below — already fixes the actual bug (a navigation's
            # new URL reaching the picker); extending "changed" here as well
            # is a separate, independently-reviewable UI nicety, not part of
            # the reported failure scenario.
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
        await _send_cdp(ws, 1, "Target.setDiscoverTargets", {"discover": True})
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


async def run_target_watcher(
    watcher: TargetWatcher, stop_event: asyncio.Event, *, ready_event: Optional[asyncio.Event] = None
) -> None:
    """Seed from /json/list, then prefer the passive WS; fall back to polling
    on any failure (M11's "ersatzweg"). Runs until stop_event is set.

    `ready_event` (if given) is set once the initial seed attempt — success
    OR failure — has happened, so a caller can wait for "the watcher has an
    opinion now" instead of racing it: without this, the WS handler's first
    `targets` message always went out before `_list_page_targets()`'s HTTP
    round-trip finished, so the client's very first push was always the
    empty-looking `{targets: []}` (finding: this also double-counted as the
    "no page open" status appearing on every single panel open)."""
    try:
        watcher.replace_from_list(await _list_page_targets())
        watcher.notify()
    except Exception as e:
        logger.info("browser_live watcher: initial /json/list failed: %s", e)
    finally:
        if ready_event is not None:
            ready_event.set()

    while not stop_event.is_set():
        try:
            await _watch_targets_browser_ws(watcher, stop_event)
        except Exception as e:
            if stop_event.is_set():
                return
            logger.info("browser_live watcher: browser WS unavailable (%s), polling instead", e)
            await _watch_targets_poll(watcher, stop_event)


class _OwnedIdsCache:
    """Short-TTL cache in front of `_gateway_owned_ids` — the WS loop below
    consults this on nearly every iteration (≤1s cadence), and hitting the
    gateway's HTTP endpoint that often for a value that only meaningfully
    changes on a tab create/close/navigate would be pure waste. None (cache
    miss or gateway down) always means "unfiltered", never "empty"."""

    def __init__(self, agent_slug: str, *, ttl: float = 1.5, now_fn=time.monotonic):
        self._slug = agent_slug
        self._ttl = ttl
        self._now = now_fn
        self._fetched_at = 0.0
        self._value: Optional[set[str]] = None
        self._assigned_at = 0.0
        self._assigned: Optional[set[str]] = None

    @property
    def ttl(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        """Force the next `get()` to re-fetch instead of serving a stale
        value. Called whenever the browser-level watcher reports a target
        change: Chromium fires `targetCreated` to the watcher (which then
        wakes the WS loop via `switch_requested`) before cdp-gateway has
        necessarily caught up with its own `Target.createTarget` bookkeeping
        for that same tab — so the FIRST re-check right after a watcher
        event can still legitimately get the gateway's old owned-ids answer.
        That is why the WS loop ALSO gets a bounded wait (see `ttl` above)
        so it keeps re-checking after this invalidation, instead of trusting
        a single post-invalidation fetch that may itself be stale."""
        self._fetched_at = 0.0
        self._assigned_at = 0.0

    async def get(self) -> Optional[set[str]]:
        if self._now() - self._fetched_at < self._ttl:
            return self._value
        self._value = await _gateway_owned_ids(self._slug)
        self._fetched_at = self._now()
        return self._value

    async def get_assigned(self) -> Optional[set[str]]:
        """Ids attributed to ANY agent (same TTL) — only needed when this
        agent owns no open tab, to tell "every tab is someone else's" apart
        from "some tab is nobody's" (see `_apply_scope`)."""
        if self._now() - self._assigned_at < self._ttl:
            return self._assigned
        self._assigned = await _gateway_assigned_ids()
        self._assigned_at = self._now()
        return self._assigned


@router.websocket("/ws")
async def browser_live_ws(
    websocket: WebSocket,
    token: Optional[str] = None,
    target: Optional[str] = None,
    ticket: Optional[str] = None,
    follow: int = 1,
    agent_id: Optional[str] = None,
):
    """Stream JPEG screencast frames of one agent-browser page to the client,
    optionally following whichever page is currently active.

    Auth: single-use stream ticket via ?ticket= (WebSocket can't send
    headers); legacy ?token=<jwt> only with ALLOW_QUERY_TOKEN_AUTH.
    ?target=<cdp target id> picks a starting page (default = active/newest).
    ?agent_id=<agent uuid> (bauplan.md PR B1) scopes everything above — the
    target list, "active"/follow and the no-open-page state — to that
    agent's own tabs via cdp-gateway (:9300). Omitted, an id that doesn't
    resolve, or the gateway being unreachable all fall back to showing
    every tab in the shared browser (today's behaviour, unchanged).
    ?follow=1 (default) keeps switching to whichever page becomes active;
    a client {"select": "<id>"} message turns following off for that
    connection.

    Server → client messages:
      {"type": "frame", "data": "<base64 jpeg>", "metadata": {...}}
      {"type": "attached", "target": {"id", "title", "url"}}
      {"type": "targets", "targets": [...], "activeId": "...", "followedId": "..."}
      {"type": "status", "code": "no_page"|"connect_error"|"stream_error"}
        A machine-readable code, never a free-text message — the client is
        bilingual (i18n) and must never render server English verbatim
        (finding: it used to send a hardcoded English sentence that showed
        up untranslated in the German UI).
      {"type": "status", "code": "scope_unavailable", "active": bool}
        Only ever sent when `agent_id` was given at all. `active: true` means
        attribution is down right now (gateway unreachable, or the agent
        couldn't be resolved) and the panel is silently showing EVERY tab
        even though the toolbar toggle still names this one agent — the UI
        must say so. `active: false` means attribution came back (sent once,
        on the transition back).
      {"type": "status", "code": "unassigned_fallback", "active": bool, "count": int}
        Also scoped panels only, sent first thing and on every change.
        `active: true`: the agent owns no open tab, but `count` tabs are not
        assigned to any agent, so the panel shows EVERY tab (`targets`
        entries then carry `"unassigned": true` where it applies) instead of
        a misleading "no open tab" (see `_apply_scope`).

    Client → server messages are steering only, never forwarded to Chromium:
      {"follow": true|false}, {"select": "<target id>"}
    """
    from app.auth import authenticate_websocket

    if not await authenticate_websocket(websocket, token=token, ticket=ticket):
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()

    # bauplan.md PR B1: when the panel is opened from a specific agent's
    # chat, scope it to that agent's own tabs via cdp-gateway. `owned_cache`
    # stays None (meaning "show everything", the pre-B1 behaviour) whenever
    # agent_id is absent, unresolvable, or the gateway can't be reached —
    # per-agent attribution degrading is never allowed to make the panel
    # itself unusable.
    owned_cache: Optional[_OwnedIdsCache] = None
    # True whenever the CALLER asked for a scoped panel (?agent_id= was
    # given) — independent of whether that scope actually resolved. Used
    # below to tell "not scoped at all" (never send the hint) apart from
    # "scoped, but attribution is unavailable right now" (finding: the
    # panel used to fall back to showing everything in that case with NO
    # signal to the operator — the toolbar toggle still said the scoped
    # agent's name and follow could silently jump to a foreign tab).
    scope_requested = bool(agent_id)
    if agent_id:
        slug = await _agent_slug(agent_id)
        if slug:
            owned_cache = _OwnedIdsCache(slug)

    async def _owned_ids() -> Optional[set[str]]:
        return await owned_cache.get() if owned_cache is not None else None

    async def _scope_unavailable() -> bool:
        return scope_requested and (await _owned_ids()) is None

    watcher = TargetWatcher()
    stop_event = asyncio.Event()
    state = {"followed_id": target, "following": bool(follow)}
    switch_requested = asyncio.Event()
    watcher_ready = asyncio.Event()

    def on_targets_change(targets: list[dict], active_id: Optional[str]) -> None:
        # Called synchronously from the watcher task, same event loop — no
        # cross-thread handoff needed (TargetWatcher never runs in a
        # separate thread). attach_and_stream re-reads watcher state itself
        # once woken, so nothing needs to be handed over here.
        #
        # Invalidate the owned-ids cache on every watcher change (medium
        # finding): Chromium's `targetCreated` reaches this watcher before
        # cdp-gateway has necessarily recorded that same `createTarget` —
        # without this, a scoped panel could keep serving the pre-create
        # owned-ids set for up to the full cache TTL and silently drop the
        # agent's brand-new tab until some unrelated later tab event forced
        # a re-check.
        if owned_cache is not None:
            owned_cache.invalidate()
        switch_requested.set()

    watcher.on_change = on_targets_change
    watcher_task = asyncio.create_task(run_target_watcher(watcher, stop_event, ready_event=watcher_ready))

    # Last `targets` payload actually sent, so a resend only goes out when
    # something in it changed — the no-pages branch used to re-send the
    # same empty list (and the same status) every single poll tick.
    _last_targets_sig: dict = {"sig": None}

    def _active_id_of(pages: list[dict]) -> Optional[str]:
        """Same preference TargetWatcher.active_id() applies (skip a blank
        tab unless it's the only one) — reimplemented here because it must
        run on a FILTERED subset when agent_id scoping is on, not on the
        watcher's full (unfiltered) target set."""
        if not pages:
            return None
        non_blank = [p for p in pages if p.get("url") not in _BLANK_URLS]
        pool = non_blank or pages
        return max(pool, key=lambda p: p["last_active_at"])["id"]

    # Ids of the unassigned pages while the unassigned fallback is on (see
    # `_apply_scope`), else None — refreshed by every `_visible_pages()` call.
    scope_view: dict = {"unassigned": None}

    async def _visible_pages() -> list[dict]:
        all_pages = watcher.targets()
        owned = await _owned_ids()
        assigned: Optional[set[str]] = None
        if owned is not None and owned_cache is not None and not any(p["id"] in owned for p in all_pages):
            assigned = await owned_cache.get_assigned()
        pages, scope_view["unassigned"] = _apply_scope(all_pages, owned, assigned)
        return pages

    async def send_targets_message() -> None:
        pages = await _visible_pages()
        unassigned = scope_view["unassigned"] or set()
        targets_list = []
        for t in pages:
            row = {"id": t["id"], "title": t["title"], "url": t["url"]}
            if t["id"] in unassigned:
                row["unassigned"] = True
            targets_list.append(row)
        active_id = _active_id_of(pages)
        followed_id = state["followed_id"]
        # url + title are part of the signature, not just the id tuple — a
        # navigation inside the shown tab (the common case: the agent works
        # in one tab) changes neither the set of ids nor active/followed, so
        # without this a `targets` push never goes out and the picker/URL
        # row under the header keep showing the page the tab had before it
        # navigated for as long as the panel stays open (finding, round 4).
        sig = (
            tuple((t["id"], t["url"], t["title"], t.get("unassigned", False)) for t in targets_list),
            active_id,
            followed_id,
        )
        if sig == _last_targets_sig["sig"]:
            return
        _last_targets_sig["sig"] = sig
        try:
            await websocket.send_json({
                "type": "targets",
                "targets": targets_list,
                "activeId": active_id,
                "followedId": followed_id,
            })
        except Exception:
            pass

    import websockets

    msg_id = 0

    def _next_id() -> int:
        nonlocal msg_id
        msg_id += 1
        return msg_id

    async def _pump(cdp) -> None:
        while True:
            raw = await cdp.recv()
            msg = json.loads(raw)
            if msg.get("method") == "Page.screencastFrame":
                params = msg["params"]
                await _send_cdp(cdp, _next_id(), "Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                await websocket.send_json({
                    "type": "frame",
                    "data": params["data"],
                    "metadata": params.get("metadata", {}),
                })

    async def _close_cdp(cdp) -> None:
        try:
            await _send_cdp(cdp, _next_id(), "Page.stopScreencast")
        except Exception:
            pass
        try:
            await cdp.close()
        except Exception:
            pass

    async def attach_and_stream(cdp_holder: dict) -> None:
        """Keep the client WebSocket showing the right page, looping forever
        so a `select`/follow-switch — or the shown page simply closing, which
        agents do constantly as Playwright tears pages down — swaps the
        underlying CDP connection without ever closing the client socket.

        Only reconnects the CDP screencast when the TARGET actually changes
        or the current one died; a tab event elsewhere in the shared browser
        just refreshes the `targets` list without a reconnect (no spurious
        "Connecting…" flash on every background navigation).
        """
        cdp = None
        current_id: Optional[str] = None
        frame_task: Optional[asyncio.Task] = None
        # Track the last status CODE sent rather than a plain sent/not-sent
        # boolean, so a status goes out once per stretch (not every 1s poll
        # tick) but a transition like connect_error → no_page still gets
        # through. A boolean here used to gate `no_page` on its own flag, so
        # once a `connect_error` had been sent and every tab then closed,
        # `no_page` was never sent and the panel kept showing "connection
        # error" forever even though the browser had simply gone tab-less
        # (finding, round 4).
        last_status_code: Optional[str] = None

        async def _send_status(code: str) -> None:
            nonlocal last_status_code
            if code == last_status_code:
                return
            last_status_code = code
            await websocket.send_json({"type": "status", "code": code})

        # Mutable holder (not a plain bool) so the closure below can flip it
        # without a `nonlocal` declaration fighting the one `_send_status`
        # already owns on `last_status_code`. `sent` starts False so the
        # FIRST loop iteration always sends a `scope_unavailable` status
        # (even when the value happens to be False, same as the initial
        # `value`) — without this, a client that connects while a previous
        # connection's `initialData.scopeUnavailable: true` (from a REST
        # call during a brief gateway outage) is still showing never learns
        # the WS itself considers attribution fine, because "unchanged from
        # the initial False" never counted as a thing worth sending (medium
        # finding, round 5).
        last_scope_unavailable = {"value": False, "sent": False}
        # Same "send first, then on change" pattern for the unassigned
        # fallback; (active, count) so a growing/shrinking count reaches the
        # hint text too.
        last_fallback = {"value": None}

        try:
            while not stop_event.is_set():
                # Clear BEFORE reading watcher state so a notification that
                # lands while we are (re)connecting below is never lost —
                # it just re-sets the event and the next loop iteration
                # picks it up, instead of being wiped by a clear() that used
                # to run only after the reconnect finished.
                switch_requested.clear()

                # bauplan.md PR B1: when scoped to one agent, "pages" here
                # means ONLY that agent's own tabs — this is what makes the
                # picker, "no open page" empty state, and auto-follow all
                # respect the per-agent scope, on top of A1's existing
                # "follow whatever is active" behaviour.
                pages = await _visible_pages()
                live_ids = {p["id"] for p in pages}

                # Separate from `_send_status`'s connection-state codes (it
                # tracks exactly one "last code", and no_page/connect_error
                # must still win the visible banner) — this one just tells
                # the client whether the "showing only this agent" toggle it
                # displays is actually true right now.
                scope_unavailable_now = await _scope_unavailable()
                if scope_requested and (
                    not last_scope_unavailable["sent"] or scope_unavailable_now != last_scope_unavailable["value"]
                ):
                    last_scope_unavailable["value"] = scope_unavailable_now
                    last_scope_unavailable["sent"] = True
                    await websocket.send_json({
                        "type": "status", "code": "scope_unavailable",
                        "active": scope_unavailable_now,
                    })
                if scope_requested:
                    unassigned_now = scope_view["unassigned"]
                    fallback_now = (unassigned_now is not None, len(unassigned_now or ()))
                    if fallback_now != last_fallback["value"]:
                        last_fallback["value"] = fallback_now
                        await websocket.send_json({
                            "type": "status", "code": "unassigned_fallback",
                            "active": fallback_now[0], "count": fallback_now[1],
                        })

                if not pages:
                    if cdp is not None:
                        await _close_cdp(cdp)
                        if frame_task:
                            frame_task.cancel()
                        cdp, frame_task, current_id = None, None, None
                    await send_targets_message()
                    await _send_status("no_page")
                    await _wait_switch_or_timeout(1.0)
                    continue

                wanted = state["followed_id"]
                if wanted not in live_ids:
                    # The shown (or requested) tab is gone — Playwright closes
                    # pages all the time. Always fall back to the active tab,
                    # even with follow off (bauplan A1): a dead tab is never
                    # a reasonable thing to keep "showing".
                    active_now = _active_id_of(pages)
                    wanted = active_now if active_now in live_ids else pages[0]["id"]
                state["followed_id"] = wanted

                if wanted != current_id or cdp is None:
                    if cdp is not None:
                        await _close_cdp(cdp)
                        if frame_task:
                            frame_task.cancel()
                        cdp, frame_task = None, None

                    try:
                        raw_pages = await _list_page_targets()
                    except Exception as e:
                        # cdp-browser restarting / a brief network hiccup —
                        # never fatal (finding: this used to escape to the
                        # outer `except Exception` and close the client
                        # socket with "Stream ended", needing a manual
                        # Reconnect for a transient outage that resolves
                        # itself a second later).
                        logger.info("browser_live: /json/list lookup failed: %s", e)
                        await _send_status("connect_error")
                        current_id = None
                        await _wait_switch_or_timeout(1.0)
                        continue
                    raw = next((p for p in raw_pages if p.get("id") == wanted), None)
                    if raw is None:
                        # Target vanished between the watcher snapshot and
                        # this live lookup — retry next iteration rather
                        # than treating it as a fatal stream error.
                        current_id = None
                        await _wait_switch_or_timeout(0.3)
                        continue
                    ws_url = _rewrite_ws_url(raw["webSocketDebuggerUrl"], _resolve_cdp_netloc())

                    try:
                        cdp = await websockets.connect(ws_url, max_size=32 * 1024 * 1024)
                    except Exception as e:
                        logger.info("browser_live: could not attach to %s: %s", wanted, e)
                        current_id = None
                        await _wait_switch_or_timeout(0.5)
                        continue

                    cdp_holder["cdp"] = cdp
                    await _send_cdp(cdp, _next_id(), "Page.startScreencast", {
                        "format": "jpeg",
                        "quality": 70,
                        "maxWidth": 1440,
                        "maxHeight": 900,
                        "everyNthFrame": 1,
                    })
                    await websocket.send_json({
                        "type": "attached",
                        "target": {"id": raw.get("id"), "title": raw.get("title"), "url": raw.get("url")},
                    })
                    current_id = wanted
                    frame_task = asyncio.create_task(_pump(cdp))

                await send_targets_message()

                # Scoped panels get a bounded wait (about the owned-ids cache
                # TTL) instead of waiting forever for the next unrelated
                # event: a watcher-triggered invalidate() can still hit the
                # gateway before IT has caught up with its own createTarget
                # bookkeeping, so the re-check right after invalidation may
                # itself still return the old set. Without this timeout
                # nothing re-checks again until some later, unrelated tab
                # event happens to fire — the scoped view then drops the
                # agent's new tab the same way A1 fixed for the unscoped one
                # (medium finding).
                wait_kwargs = {"return_when": asyncio.FIRST_COMPLETED}
                if owned_cache is not None:
                    wait_kwargs["timeout"] = owned_cache.ttl
                switch_wait = asyncio.create_task(switch_requested.wait())
                done, pending = await asyncio.wait({frame_task, switch_wait}, **wait_kwargs)
                if not done:
                    # Timed out with nothing resolved — treat exactly like a
                    # switch signal so the loop re-reads `_visible_pages()`
                    # (and thus the freshly-invalidated/refetched owned ids)
                    # on its next iteration, without tearing down the live
                    # screencast. Also re-run the same "jump to active" step
                    # an explicit switch gets below: a tab the gateway only
                    # just attributed to this agent on the SECOND re-check
                    # must still be able to grab follow, not just appear in
                    # the picker.
                    if switch_wait in pending:
                        switch_wait.cancel()
                    if state["following"]:
                        active_now = _active_id_of(await _visible_pages())
                        if active_now:
                            state["followed_id"] = active_now
                    continue

                if switch_wait in done:
                    # frame_task is NOT cancelled here — a tab event
                    # elsewhere (or a {"follow": true} that doesn't actually
                    # change the shown target) must not tear down a healthy
                    # screencast. Cancelling it was the earlier bug: it
                    # silently killed the pump, and since `Task.exception()`
                    # raises (not returns) `CancelledError` — a BaseException,
                    # not an Exception — on a self-cancelled task, that
                    # propagated straight past every `except Exception` in
                    # this function and ended the WebSocket with no log line
                    # at all the very next time this loop ran.
                    if switch_wait in pending:
                        switch_wait.cancel()
                    if state["following"]:
                        active_now = _active_id_of(await _visible_pages())
                        if active_now:
                            state["followed_id"] = active_now
                    continue

                if switch_wait in pending:
                    switch_wait.cancel()

                # frame_task finished — the page connection closed/died.
                # Never fatal: drop it and let the next iteration decide
                # (re-attach to the same id if it's still live, otherwise
                # fall through to active_id / "No open page"). Task.exception()
                # RAISES (doesn't return) CancelledError for a task that
                # ended via cancellation — a BaseException, so it must be
                # caught explicitly, not just via `except Exception`.
                try:
                    frame_task.exception()
                except (Exception, asyncio.CancelledError):
                    pass
                if cdp is not None:
                    try:
                        await cdp.close()
                    except Exception:
                        pass
                cdp, frame_task, current_id = None, None, None
                if stop_event.is_set():
                    return
        except WebSocketDisconnect:
            raise
        except Exception as e:
            logger.info("browser_live stream ended: %s", e)
            try:
                await websocket.send_json({"type": "status", "code": "stream_error"})
            except Exception:
                pass
            return
        finally:
            if frame_task is not None:
                frame_task.cancel()
                try:
                    await frame_task
                except (Exception, asyncio.CancelledError):
                    pass
            if cdp is not None:
                await _close_cdp(cdp)

    async def _wait_switch_or_timeout(timeout: float) -> None:
        try:
            await asyncio.wait_for(switch_requested.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

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
                if state["following"]:
                    # Jump to the active tab right away instead of waiting
                    # for the next unrelated tab event (finding: turning
                    # Follow back on didn't move the stream until some
                    # later navigation elsewhere happened to fire).
                    switch_requested.set()
            if "select" in msg and isinstance(msg["select"], str):
                state["followed_id"] = msg["select"]
                state["following"] = False
                switch_requested.set()

    cdp_holder: dict = {}
    try:
        # Wait for the watcher's initial seed before the first `targets`
        # push — otherwise it always went out as the hollow {targets: []}
        # before `_list_page_targets()`'s HTTP round-trip had a chance to
        # finish (finding: this doubled up with the "no open page" status
        # flashing on every single panel open, even with tabs open the
        # whole time). Bounded so a dead cdp-browser never hangs the socket.
        try:
            await asyncio.wait_for(watcher_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
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
            # Machine-readable code only — this branch used to send
            # {"message": str(e)[:200]}, free server-side English the
            # bilingual client can't translate and silently ignores anyway
            # (finding, round 4: broke the code-only contract this
            # docstring already promises for every other status send).
            await websocket.send_json({"type": "status", "code": "stream_error"})
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
