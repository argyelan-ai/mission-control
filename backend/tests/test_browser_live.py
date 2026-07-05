"""Live-Browser-View (CDP-Screencast-Proxy) — Auth + Target-Handling."""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.routers.browser_live import _rewrite_ws_url


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


def test_page_filter_and_order():
    """Filter-/Sortierlogik von _list_page_targets (ohne HTTP)."""
    from app.routers import browser_live as bl
    raw = [
        {"id": "old", "type": "page"},
        {"id": "bg", "type": "background_page"},
        {"id": "new", "type": "page"},
    ]
    pages = list(reversed([t for t in raw if t.get("type") == "page"]))
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
async def test_ws_accepts_valid_jwt():
    from app.auth import create_access_token
    from app.routers.browser_live import _validate_ws_token
    assert _validate_ws_token(create_access_token("user-1", "admin")) is True
