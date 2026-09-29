"""Core hook plumbing for verticals (ADR-044): home_alert_providers.

GET /api/v1/system/alerts collects alerts from registered providers so a
vertical can put "this feed went quiet" on the Home page. Core-level test:
runs in stripped installations too (no vertical needed).
"""
import pytest

from app.verticals import hooks as vertical_hooks


@pytest.fixture(autouse=True)
def _clean_home_alert_registry():
    # Installed verticals (e.g. a private overlay) register providers when
    # app.main is first imported — import it NOW so the clear below wins.
    import app.main  # noqa: F401

    saved = list(vertical_hooks.home_alert_providers)
    vertical_hooks.home_alert_providers[:] = []
    yield
    vertical_hooks.home_alert_providers[:] = saved


@pytest.mark.asyncio
async def test_alerts_empty_without_providers(auth_client):
    resp = await auth_client.get("/api/v1/system/alerts")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"alerts": []}


@pytest.mark.asyncio
async def test_alerts_include_registered_provider_alert(auth_client):
    async def provider(session):
        return [{
            "id": "feed-quiet",
            "severity": "critical",
            "title": {"de": "Feed still", "en": "Feed quiet"},
            "detail": "seit 30 Tagen nichts Neues",
            "href": "/feeds",
        }]

    vertical_hooks.home_alert_providers.append(provider)

    resp = await auth_client.get("/api/v1/system/alerts")
    assert resp.status_code == 200, resp.text
    assert resp.json()["alerts"] == [{
        "id": "feed-quiet",
        "severity": "critical",
        "title": {"de": "Feed still", "en": "Feed quiet"},
        "detail": "seit 30 Tagen nichts Neues",
        "href": "/feeds",
    }]


@pytest.mark.asyncio
async def test_raising_provider_is_skipped_others_still_show(auth_client):
    async def broken(session):
        raise RuntimeError("boom")

    async def ok(session):
        return [{"id": "a", "title": "A"}]

    vertical_hooks.home_alert_providers.extend([broken, ok])

    resp = await auth_client.get("/api/v1/system/alerts")
    assert resp.status_code == 200, resp.text
    alerts = resp.json()["alerts"]
    assert [a["id"] for a in alerts] == ["a"]
    # Defaults: unknown/missing severity -> warning, no href/detail.
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["href"] is None
    assert alerts[0]["detail"] is None


@pytest.mark.asyncio
async def test_malformed_alerts_dropped_and_offsite_href_removed(auth_client):
    async def provider(session):
        return [
            "not-a-dict",
            {"title": "no id"},
            {"id": "no-title"},
            {"id": "x", "title": "X", "href": "https://evil.example/"},
            {"id": "y", "title": "Y", "href": "//evil.example/"},
        ]

    vertical_hooks.home_alert_providers.append(provider)

    resp = await auth_client.get("/api/v1/system/alerts")
    alerts = resp.json()["alerts"]
    assert [a["id"] for a in alerts] == ["x", "y"]
    assert all(a["href"] is None for a in alerts)


@pytest.mark.asyncio
async def test_alerts_require_login(client):
    resp = await client.get("/api/v1/system/alerts")
    assert resp.status_code in (401, 403)
