"""E0 page usage — route beacon (counter per day and route, no user data)."""
from datetime import datetime, timezone

import pytest

from app.services.usage_pages import normalize_route, page_views_by_week, record_page_view

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)  # Thursday, 2026-W39


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/", "/"),
        ("/sessions", "/sessions"),
        ("/agents/:id", "/agents/:id"),
        ("/agents/3f2a6c1e-0b7d-4a51-9a53-1b6f0f1e2d3c", "/agents/:id"),
        ("/schedule/1234", "/schedule/:id"),
        ("/tasks?view=board#x", "/tasks"),
        ("/memory/graph/", "/memory/graph"),
        ("/Sessions", "/sessions"),
        ("/tasks/abcdef0123456789", "/tasks/:id"),
    ],
)
def test_normalize_route_keeps_patterns_only(raw, expected):
    assert normalize_route(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "sessions", "/a b", "/x/<script>", "/" + "a" * 80, "/a/b/c/d/e", "https://evil.example/x", "/ümlaut"],
)
def test_normalize_route_rejects_free_text(raw):
    assert normalize_route(raw) is None


async def test_views_are_counted_per_route_and_iso_week(fake_redis):
    await record_page_view(fake_redis, "/sessions", now=datetime(2026, 9, 15, tzinfo=timezone.utc))
    await record_page_view(fake_redis, "/sessions", now=NOW)
    await record_page_view(fake_redis, "/sessions", now=NOW)
    await record_page_view(fake_redis, "/agents/:id", now=NOW)

    result = await page_views_by_week(fake_redis, weeks=2, now=NOW)

    assert [w["week"] for w in result["weeks"]] == ["2026-W38", "2026-W39"]
    w38, w39 = result["weeks"]
    assert w38["routes"] == {"/sessions": 1}
    assert w39["routes"] == {"/sessions": 2, "/agents/:id": 1}
    assert w39["total"] == 3


async def test_distinct_routes_per_day_are_capped(fake_redis, monkeypatch):
    monkeypatch.setattr("app.services.usage_pages.MAX_ROUTES_PER_DAY", 2)
    for route in ("/a", "/b", "/c"):
        await record_page_view(fake_redis, route, now=NOW)
    await record_page_view(fake_redis, "/a", now=NOW)  # known route still counts

    result = await page_views_by_week(fake_redis, weeks=1, now=NOW)

    assert result["weeks"][0]["routes"] == {"/a": 2, "/b": 1}
    assert result["weeks"][0]["dropped"] == 1


async def test_beacon_endpoint_counts_and_reads_back(auth_client):
    resp = await auth_client.post("/api/v1/usage/page", json={"route": "/agents/3f2a6c1e-0b7d-4a51-9a53-1b6f0f1e2d3c"})
    assert resp.status_code == 204

    body = (await auth_client.get("/api/v1/usage/pages?weeks=1")).json()
    assert body["weeks"][-1]["routes"] == {"/agents/:id": 1}


async def test_beacon_endpoint_rejects_free_text(auth_client):
    resp = await auth_client.post("/api/v1/usage/page", json={"route": "/a b"})
    assert resp.status_code == 422


async def test_beacon_endpoints_require_login(client):
    assert (await client.post("/api/v1/usage/page", json={"route": "/"})).status_code in (401, 403)
    assert (await client.get("/api/v1/usage/pages")).status_code in (401, 403)
