"""Update-Check (B3, 2026-07-02): GitHub-Releases-Vergleich + Endpoint."""
import pytest
from httpx import AsyncClient

from app.services.update_check import get_latest_release, is_newer


def test_is_newer_semver_compare():
    assert is_newer("v0.2.0", "0.1.0") is True
    assert is_newer("0.1.1", "0.1.0") is True
    assert is_newer("v0.1.0", "0.1.0") is False
    assert is_newer("v0.0.9", "0.1.0") is False
    # Unparsebares darf NIE einen Update-Banner ausloesen
    assert is_newer(None, "0.1.0") is False
    assert is_newer("nightly", "0.1.0") is False
    assert is_newer("v1.0.0", None) is False


@pytest.fixture
def _patched_redis(fake_redis, monkeypatch):
    """update_check ruft get_redis() direkt (nicht via Depends) auf."""
    import app.services.update_check as uc

    async def _fake():
        return fake_redis

    monkeypatch.setattr(uc, "get_redis", _fake)
    return fake_redis


@pytest.mark.asyncio
async def test_get_latest_release_caches_and_swallows_errors(_patched_redis):
    calls = {"n": 0}

    async def fetch_ok():
        calls["n"] += 1
        return {"tag_name": "v9.9.9", "html_url": "https://example.com/r"}

    first = await get_latest_release(_fetch=fetch_ok)
    assert first == {"tag": "v9.9.9", "url": "https://example.com/r"}
    # Zweiter Aufruf kommt aus dem Cache — fetch wird nicht erneut gerufen
    second = await get_latest_release(_fetch=fetch_ok)
    assert second == first
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_get_latest_release_error_is_silent(_patched_redis):
    async def fetch_boom():
        raise RuntimeError("rate limited")

    info = await get_latest_release(_fetch=fetch_boom)
    assert info == {"tag": None, "url": None}


@pytest.mark.asyncio
async def test_version_endpoint(auth_client: AsyncClient, fake_redis, monkeypatch):
    import app.routers.system  # noqa: F401 — Endpoint-Modul
    from app import config

    async def fake_latest(_fetch=None):
        return {"tag": "v99.0.0", "url": "https://example.com/rel"}

    import app.services.update_check as uc
    monkeypatch.setattr(uc, "get_latest_release", fake_latest)

    r = await auth_client.get("/api/v1/system/version")
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == config.settings.app_version
    assert body["latest"] == "v99.0.0"
    assert body["update_available"] is True
    assert body["release_url"] == "https://example.com/rel"
