"""Phase 16 — Tests for DB-backed runtime registry.

D-01/D-03: GET /runtimes reads from the DB (not JSON).
"""
from unittest.mock import patch

import pytest

from app.models.runtime import Runtime


async def _stub_state(*_args, **_kwargs):
    """Replacement for runtime_manager.get_runtime_state — bypasses SSH."""
    return {"state": "ready", "http_reachable": True, "container_status": None}


# ── list_db_runtimes Helper ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_db_runtimes_empty(async_session):
    """Empty DB → empty list."""
    from app.services.runtime_manager import list_db_runtimes

    result = await list_db_runtimes(async_session)
    assert result == []


@pytest.mark.asyncio
async def test_list_db_runtimes_sorted_by_ui_order(async_session):
    """Two runtimes → both returned, sorted by ui_order."""
    from app.services.runtime_manager import list_db_runtimes

    rt2 = Runtime(
        slug="b-second",
        display_name="Second",
        runtime_type="lmstudio",
        endpoint="http://localhost:1235/v1",
        ui_order=2,
        enabled=True,
    )
    rt1 = Runtime(
        slug="a-first",
        display_name="First",
        runtime_type="lmstudio",
        endpoint="http://localhost:1234/v1",
        ui_order=1,
        enabled=True,
    )
    # Insert in reverse order to verify sort
    async_session.add(rt2)
    async_session.add(rt1)
    await async_session.commit()

    result = await list_db_runtimes(async_session)
    assert len(result) == 2
    assert result[0].slug == "a-first"
    assert result[1].slug == "b-second"


@pytest.mark.asyncio
async def test_list_db_runtimes_includes_disabled(async_session):
    """Disabled runtimes are returned too (filtering happens in the router)."""
    from app.services.runtime_manager import list_db_runtimes

    rt_enabled = Runtime(
        slug="enabled-rt",
        display_name="Enabled",
        runtime_type="lmstudio",
        endpoint="http://localhost:1234/v1",
        ui_order=1,
        enabled=True,
    )
    rt_disabled = Runtime(
        slug="disabled-rt",
        display_name="Disabled",
        runtime_type="lmstudio",
        endpoint="http://localhost:1235/v1",
        ui_order=2,
        enabled=False,
    )
    async_session.add(rt_enabled)
    async_session.add(rt_disabled)
    await async_session.commit()

    result = await list_db_runtimes(async_session)
    slugs = {rt.slug for rt in result}
    assert slugs == {"enabled-rt", "disabled-rt"}


# ── GET /api/v1/runtimes — DB-backed ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_runtimes_returns_enabled_from_db(async_session, auth_client):
    """GET /api/v1/runtimes returns only enabled runtimes from the DB."""
    rt = Runtime(
        slug="db-only-rt",
        display_name="DB Only Runtime",
        runtime_type="openai_compatible",
        endpoint="http://example.com/v1",
        ui_order=5,
        enabled=True,
    )
    rt_disabled = Runtime(
        slug="db-disabled-rt",
        display_name="Disabled",
        runtime_type="openai_compatible",
        endpoint="http://example.com/v1",
        ui_order=6,
        enabled=False,
    )
    async_session.add(rt)
    async_session.add(rt_disabled)
    await async_session.commit()

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    slugs = {r["slug"] for r in data["runtimes"]}
    assert "db-only-rt" in slugs
    assert "db-disabled-rt" not in slugs


@pytest.mark.asyncio
async def test_get_runtimes_uses_db_not_json(async_session, auth_client):
    """A runtime that exists ONLY in the DB (slug not in JSON) shows up
    in GET /runtimes — proof that the DB is the source, not runtimes.json."""
    rt = Runtime(
        slug="phase-16-fresh-runtime",
        display_name="Phase 16 Fresh",
        runtime_type="openai_compatible",
        endpoint="http://localhost:9999/v1",
        ui_order=99,
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    assert resp.status_code == 200
    slugs = {r["slug"] for r in resp.json()["runtimes"]}
    assert "phase-16-fresh-runtime" in slugs


@pytest.mark.asyncio
async def test_get_runtime_by_slug_from_db(async_session, auth_client):
    """GET /api/v1/runtimes/{slug} returns 200 for an existing slug."""
    rt = Runtime(
        slug="single-rt",
        display_name="Single",
        runtime_type="openai_compatible",
        endpoint="http://localhost:9000/v1",
        ui_order=1,
        enabled=True,
    )
    async_session.add(rt)
    await async_session.commit()

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes/single-rt")

    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "single-rt"
    assert data["display_name"] == "Single"


@pytest.mark.asyncio
async def test_get_runtime_unknown_returns_404(async_session, auth_client):
    """GET /api/v1/runtimes/{unknown} returns 404."""
    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=_stub_state,
    ):
        resp = await auth_client.get("/api/v1/runtimes/no-such-runtime")

    assert resp.status_code == 404
