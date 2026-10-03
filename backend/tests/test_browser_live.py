"""Live-Browser-View (CDP-Screencast-Proxy) — Auth, Target-Handling, Follow."""
import re
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.routers.browser_live import TargetWatcher, _rewrite_ws_url


def test_rewrite_ws_url_replaces_host():
    out = _rewrite_ws_url("ws://127.0.0.1:9222/devtools/page/AB12", "10.0.0.7:9223")
    assert out == "ws://10.0.0.7:9223/devtools/page/AB12"


def test_resolve_cdp_netloc_falls_back_to_name(monkeypatch):
    """Nicht auflösbarer Hostname → Name behalten (Fehler kommt dann laut
    vom eigentlichen Call, nicht leise vom Resolver)."""
    from app.routers.browser_live import _resolve_cdp_netloc
    assert _resolve_cdp_netloc("http://definitely-not-resolvable-xyz:9223").endswith(":9223")


def test_resolve_cdp_netloc_resolves_ip(monkeypatch):
    import socket as _socket
    from app.routers import browser_live as bl
    monkeypatch.setattr(_socket, "gethostbyname", lambda h: "172.20.0.9")
    assert bl._resolve_cdp_netloc("http://cdp-browser:9223") == "172.20.0.9:9223"


@pytest.mark.asyncio
async def test_targets_endpoint_lists_pages(auth_client: AsyncClient):
    # _list_page_targets liefert bereits page-gefiltert + neueste zuerst
    pages = [
        {"id": "new", "type": "page", "title": "New", "url": "http://b", "webSocketDebuggerUrl": "ws://x/2"},
        {"id": "old", "type": "page", "title": "Old", "url": "http://a", "webSocketDebuggerUrl": "ws://x/1"},
    ]
    with patch("app.routers.browser_live._list_page_targets", new=AsyncMock(return_value=pages)):
        r = await auth_client.get("/api/v1/browser-live/targets")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()] == ["new", "old"]
    assert "webSocketDebuggerUrl" not in r.json()[0]  # interne URL nicht leaken


@pytest.mark.asyncio
async def test_list_page_targets_keeps_chromium_newest_first_order(monkeypatch):
    """Ruft die ECHTE _list_page_targets auf (HTTP gemockt). Chromium liefert
    /json/list neueste-zuerst (live geprueft 03.10.: frisch per /json/new
    geoeffneter Tab stand VOR dem alten about:blank). Die Liste darf weder
    umgedreht werden (Bug bis 03.10.: Panel oeffnete immer den aeltesten Tab)
    noch Nicht-Seiten enthalten. Der alte Test baute die Logik nur nach und
    pruefte damit nichts."""
    from app.routers import browser_live as bl

    raw = [
        {"id": "new", "type": "page"},
        {"id": "bg", "type": "background_page"},
        {"id": "sw", "type": "service_worker"},
        {"id": "old", "type": "page"},
    ]

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return raw

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            assert url.endswith("/json/list")
            return _Resp()

    monkeypatch.setattr(bl.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(bl, "_resolve_cdp_netloc", lambda base_url=None: "10.0.0.7:9223")
    pages = await bl._list_page_targets()
    assert [t["id"] for t in pages] == ["new", "old"]


@pytest.mark.asyncio
async def test_targets_endpoint_502_when_browser_down(auth_client: AsyncClient):
    with patch("app.routers.browser_live._list_page_targets", new=AsyncMock(side_effect=OSError("refused"))):
        r = await auth_client.get("/api/v1/browser-live/targets")
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_ws_rejects_missing_token():
    from app.routers.browser_live import _validate_ws_token
    assert _validate_ws_token(None) is False
    assert _validate_ws_token("garbage") is False


@pytest.mark.asyncio
async def test_ws_query_jwt_rejected_by_default():
    """Login JWT in the URL is off by default (stream tickets instead)."""
    from app.auth import create_access_token
    from app.routers.browser_live import _validate_ws_token
    assert _validate_ws_token(create_access_token("user-1", "admin")) is False


@pytest.mark.asyncio
async def test_ws_accepts_valid_jwt_with_legacy_switch(monkeypatch):
    import app.config
    from app.auth import create_access_token
    from app.routers.browser_live import _validate_ws_token
    monkeypatch.setattr(app.config.settings, "allow_query_token_auth", True)
    assert _validate_ws_token(create_access_token("user-1", "admin")) is True


# ── TargetWatcher (PR A1: panel follows the active tab) ──────────────────────


def _clock():
    """Deterministic monotonic clock for watcher tests: each call advances by 1."""
    t = [0.0]

    def tick():
        t[0] += 1.0
        return t[0]

    return tick


def test_watcher_target_created_with_url_becomes_active():
    w = TargetWatcher(now_fn=_clock())
    assert w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "a", "type": "page", "title": "A", "url": "https://a.example",
    }}) is True
    assert w.active_id() == "a"
    assert w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "b", "type": "page", "title": "B", "url": "https://b.example",
    }}) is True
    assert w.active_id() == "b"  # newest created wins


def test_watcher_blank_tab_does_not_win_against_real_tab():
    clock = _clock()
    w = TargetWatcher(now_fn=clock)
    w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "real", "type": "page", "title": "Real", "url": "https://real.example",
    }})
    # A fresh about:blank tab created AFTER the real one (Playwright's
    # pre-navigation placeholder, M11) must not steal "active".
    w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "blank", "type": "page", "title": "", "url": "about:blank",
    }})
    assert w.active_id() == "real"


def test_watcher_blank_tab_wins_when_it_is_the_only_tab():
    w = TargetWatcher(now_fn=_clock())
    w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "blank", "type": "page", "title": "", "url": "about:blank",
    }})
    assert w.active_id() == "blank"


def test_watcher_pure_title_change_does_not_move_active():
    w = TargetWatcher(now_fn=_clock())
    w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "a", "type": "page", "title": "A", "url": "https://a.example",
    }})
    w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "b", "type": "page", "title": "B", "url": "https://b.example",
    }})
    # "a" becomes active again only via a URL change, never via title alone.
    changed = w.handle_event("targetInfoChanged", {"targetInfo": {
        "targetId": "a", "type": "page", "title": "A — updated", "url": "https://a.example",
    }})
    assert changed is False
    assert w.active_id() == "b"


def test_watcher_url_change_on_existing_tab_makes_it_active():
    w = TargetWatcher(now_fn=_clock())
    w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "a", "type": "page", "title": "A", "url": "https://a.example",
    }})
    w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "b", "type": "page", "title": "B", "url": "https://b.example",
    }})
    changed = w.handle_event("targetInfoChanged", {"targetInfo": {
        "targetId": "a", "type": "page", "title": "A", "url": "https://a.example/other",
    }})
    assert changed is True
    assert w.active_id() == "a"


def test_watcher_destroyed_target_is_dropped():
    w = TargetWatcher(now_fn=_clock())
    w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "a", "type": "page", "title": "A", "url": "https://a.example",
    }})
    assert w.handle_event("targetDestroyed", {"targetId": "a"}) is True
    assert w.targets() == []
    assert w.active_id() is None


def test_watcher_non_page_targets_are_ignored():
    w = TargetWatcher(now_fn=_clock())
    assert w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "sw", "type": "service_worker", "title": "", "url": "",
    }}) is False
    assert w.targets() == []


def test_watcher_replace_from_list_is_a_fallback_for_polling():
    """Ersatzweg (browser-WS down): /json/list polling merges into the same
    state, new ids count as activity."""
    w = TargetWatcher(now_fn=_clock())
    assert w.replace_from_list([
        {"id": "a", "title": "A", "url": "https://a.example"},
    ]) is True
    assert w.active_id() == "a"
    assert w.replace_from_list([
        {"id": "a", "title": "A", "url": "https://a.example"},
        {"id": "b", "title": "B", "url": "https://b.example"},
    ]) is True
    assert w.active_id() == "b"
    # Closing a tab (disappears from /json/list) is also a change.
    assert w.replace_from_list([{"id": "b", "title": "B", "url": "https://b.example"}]) is True
    assert [t["id"] for t in w.targets()] == ["b"]


def test_watcher_on_change_callback_fires_only_on_real_change():
    w = TargetWatcher(now_fn=_clock())
    calls = []
    w.on_change = lambda targets, active_id: calls.append(active_id)
    w.handle_event("targetCreated", {"targetInfo": {
        "targetId": "a", "type": "page", "title": "A", "url": "https://a.example",
    }})
    # Pure title update on the same URL — must not be reported as a change
    # (that's exactly what lets a real navigation, not a title tweak, move
    # "active").
    if w.handle_event("targetInfoChanged", {"targetInfo": {
        "targetId": "a", "type": "page", "title": "A — renamed", "url": "https://a.example",
    }}):
        w.notify()
    assert calls == []  # notify() was never called for the no-op second event


# ── Sabotage probe: view-only guarantee ──────────────────────────────────────
# Every CDP method literal this module sends anywhere must be in the allowed
# set below. This is a static, regex-based sabotage probe rather than a live
# CDP capture (no real browser in CI) — it still fails loudly if someone adds
# an input-dispatching call, and the test is proven to catch it below.

_ALLOWED_CDP_METHODS = {
    "Target.setDiscoverTargets",
    "Page.startScreencast",
    "Page.screencastFrameAck",
    "Page.stopScreencast",
}

# Every outgoing CDP call in this module — browser-level watcher connection
# AND the per-page stream — goes through the single `_send_cdp()` choke
# point (runtime-asserts its method against ALLOWED_CDP_METHODS). The static
# regex below is a fast first line of defense that catches a literal method
# string anywhere in the source, including one passed as `_send_cdp(...)`'s
# third positional/keyword argument; the runtime assert in `_send_cdp` is
# what actually catches a method built from a variable or f-string (a static
# probe can never see that), see `test_runtime_assert_...` below.
_METHOD_LITERAL_RE = re.compile(r'"method":\s*"([A-Za-z]+\.[A-Za-z]+)"|_send_cdp\([^)]*?,\s*"([A-Za-z]+\.[A-Za-z]+)"')


def _cdp_methods_sent_by_module() -> set[str]:
    import inspect
    from app.routers import browser_live as bl

    src = inspect.getsource(bl)
    found = set()
    for a, b in _METHOD_LITERAL_RE.findall(src):
        found.add(a or b)
    return found


def test_browser_live_only_sends_view_only_cdp_methods():
    sent = _cdp_methods_sent_by_module()
    assert sent, "expected to find at least one outgoing CDP method literal"
    assert sent <= _ALLOWED_CDP_METHODS, (
        f"browser_live.py sends non-view-only CDP methods: {sent - _ALLOWED_CDP_METHODS}"
    )


def test_sabotage_probe_catches_an_input_method():
    """Proves the regex above actually works: injecting an input-dispatching
    method literal into a throwaway source string must be flagged."""
    fake_src = 'await _send_cdp(cdp, 1, "Input.dispatchMouseEvent")'
    found = set()
    for a, b in _METHOD_LITERAL_RE.findall(fake_src):
        found.add(a or b)
    assert found == {"Input.dispatchMouseEvent"}
    assert not (found <= _ALLOWED_CDP_METHODS)


@pytest.mark.asyncio
async def test_send_cdp_runtime_asserts_method_allowlist():
    """The runtime choke point catches what the static regex structurally
    cannot: a method name built from a variable, not a literal."""
    from app.routers.browser_live import _send_cdp

    class _FakeConn:
        async def send(self, data):
            pass

    not_a_literal = "Input" + "." + "dispatchMouseEvent"
    with pytest.raises(AssertionError):
        await _send_cdp(_FakeConn(), 1, not_a_literal)
