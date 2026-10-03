"""Tests for bauplan.md PR B1's `agent_id` scoping on `/browser-live/*`:
the per-agent browser panel must show only the tabs `cdp-gateway` attributes
to that agent, and must fall back to "show everything" (today's behaviour)
whenever the agent can't be resolved or the gateway is unreachable — a
gateway outage must never make the whole panel unusable.

Reuses `test_browser_live_ws_handler.py`'s fake-CDP-world harness (same
module, same patterns) rather than rebuilding it, and
`test_browser_live.py`'s plain-function style for the `/targets` REST side.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import app.config
from app.auth import create_access_token
from app.models.agent import Agent
from app.models.user import User
from app.routers import browser_live as bl
from sqlmodel.ext.asyncio.session import AsyncSession
from tests.conftest import test_engine
from tests.test_browser_live_ws_handler import (
    EventBus,
    FakeCDPWorld,
    _allow_query_token_auth,  # noqa: F401 (autouse fixture)
    _page,
    _recv_until,
    _seed_user_and_token,
    _ws,
    _ws_session_on_test_engine,  # noqa: F401 (autouse fixture)
    real_run_target_watcher,  # noqa: F401 (fixture)
)


def _make_app(world: FakeCDPWorld, events: EventBus) -> FastAPI:
    app = FastAPI()
    app.include_router(bl.router)
    from tests.test_browser_live_ws_handler import _fake_watcher_driver

    driver = _fake_watcher_driver(events)

    async def _run_target_watcher(watcher, stop_event, *, ready_event=None):
        await driver(watcher, stop_event, initial_pages=world.pages, ready_event=ready_event)

    bl.run_target_watcher = _run_target_watcher  # type: ignore[assignment]
    bl._list_page_targets = world.list_page_targets  # type: ignore[assignment]
    return app


# ── `_agent_slug`: resolve on the test's own session, same StaticPool fix
#    the WS-auth fixture above already applies, for the same reason.

@pytest.fixture(autouse=True)
def _agent_slug_session_on_test_engine(monkeypatch):
    async def _session_maker():
        return AsyncSession(test_engine, expire_on_commit=False)

    # `_agent_slug` does `async with async_session_maker() as session`, so the
    # patched name must itself be the context-manager factory, not a coroutine.
    monkeypatch.setattr(bl, "async_session_maker", lambda: AsyncSession(test_engine, expire_on_commit=False))


# ── _agent_slug ──────────────────────────────────────────────────────────
#
# These three run as real pytest-asyncio tests (not a hand-rolled
# `asyncio.run()`) so they share the SAME event loop setup_db's autouse
# fixture (tests/conftest.py) already created the schema on — a bare
# `asyncio.run()` opens an unrelated loop and silently saw "no such table:
# agents" (swallowed by `_agent_slug`'s own broad except, which is exactly
# the kind of masking that cost real time to find while building this).

@pytest.mark.asyncio
async def test_agent_slug_uses_persisted_slug_column():
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(id=agent_id, name="Sparky Two", slug="sparky-2", agent_runtime="cli-bridge"))
        await s.commit()
    with patch.object(bl, "async_session_maker", lambda: AsyncSession(test_engine, expire_on_commit=False)):
        assert await bl._agent_slug(str(agent_id)) == "sparky-2"


@pytest.mark.asyncio
async def test_agent_slug_falls_back_to_name_when_slug_column_empty():
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(id=agent_id, name="Old Agent", slug=None, agent_runtime="cli-bridge"))
        await s.commit()
    with patch.object(bl, "async_session_maker", lambda: AsyncSession(test_engine, expire_on_commit=False)):
        assert await bl._agent_slug(str(agent_id)) == "old-agent"


@pytest.mark.asyncio
async def test_agent_slug_returns_none_for_unknown_id():
    with patch.object(bl, "async_session_maker", lambda: AsyncSession(test_engine, expire_on_commit=False)):
        assert await bl._agent_slug(str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_agent_slug_returns_none_for_a_malformed_id_instead_of_raising():
    """Regression guard: a raw `agent_id` string must be converted to
    `uuid.UUID` before the DB lookup — the sqlite/UUID adapter raises
    `AttributeError: 'str' object has no attribute 'hex'` otherwise (found
    while building this: every lookup silently failed and fell through to
    "show everything" even for agents that DID exist, because the resulting
    exception was swallowed by the broad `except Exception` one line down).
    A garbage id must still resolve to None, not propagate an exception."""
    with patch.object(bl, "async_session_maker", lambda: AsyncSession(test_engine, expire_on_commit=False)):
        assert await bl._agent_slug("not-a-uuid-at-all") is None


# ── _gateway_owned_ids ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gateway_owned_ids_returns_set_from_mc_targets():
    rows = [{"targetId": "T1", "agent": "sparky"}, {"targetId": "T2", "agent": "sparky"}]

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return rows

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            assert params == {"agent": "sparky"}
            return _FakeResp()

    with patch("httpx.AsyncClient", lambda **kw: _FakeClient()):
        ids = await bl._gateway_owned_ids("sparky")
    assert ids == {"T1", "T2"}


@pytest.mark.asyncio
async def test_gateway_owned_ids_returns_none_when_gateway_unreachable():
    with patch("httpx.AsyncClient", side_effect=OSError("refused")):
        ids = await bl._gateway_owned_ids("sparky")
    assert ids is None


# ── /targets REST endpoint ───────────────────────────────────────────────

def test_targets_endpoint_filters_by_agent_when_gateway_knows_the_agent():
    app = FastAPI()
    app.include_router(bl.router)
    pages = [
        {"id": "T1", "title": "sparky's page", "url": "https://a", "type": "page"},
        {"id": "T2", "title": "boss's page", "url": "https://b", "type": "page"},
    ]
    with patch.object(bl, "_list_page_targets", new=AsyncMock(return_value=pages)), \
         patch.object(bl, "_agent_slug", new=AsyncMock(return_value="sparky")), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value={"T1"})), \
         patch("app.auth.require_user", return_value={"sub": "u1"}):
        app.dependency_overrides[bl.require_user] = lambda: {"sub": "u1"}
        with TestClient(app) as client:
            resp = client.get("/api/v1/browser-live/targets", params={"agent_id": str(uuid.uuid4())})
    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()] == ["T1"]


def test_targets_endpoint_shows_everything_when_gateway_is_down_sabotage_probe():
    """Sabotage probe for the fallback rule itself: if `_gateway_owned_ids`
    returning None were (incorrectly) treated as 'this agent owns nothing'
    instead of 'attribution unavailable', this test would see an empty list
    instead of both pages and fail."""
    app = FastAPI()
    app.include_router(bl.router)
    pages = [
        {"id": "T1", "title": "a", "url": "https://a", "type": "page"},
        {"id": "T2", "title": "b", "url": "https://b", "type": "page"},
    ]
    with patch.object(bl, "_list_page_targets", new=AsyncMock(return_value=pages)), \
         patch.object(bl, "_agent_slug", new=AsyncMock(return_value="sparky")), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value=None)):
        app.dependency_overrides[bl.require_user] = lambda: {"sub": "u1"}
        with TestClient(app) as client:
            resp = client.get("/api/v1/browser-live/targets", params={"agent_id": str(uuid.uuid4())})
    assert resp.status_code == 200
    assert {t["id"] for t in resp.json()} == {"T1", "T2"}


def test_targets_endpoint_shows_everything_when_agent_id_unresolvable():
    app = FastAPI()
    app.include_router(bl.router)
    pages = [{"id": "T1", "title": "a", "url": "https://a", "type": "page"}]
    with patch.object(bl, "_list_page_targets", new=AsyncMock(return_value=pages)), \
         patch.object(bl, "_agent_slug", new=AsyncMock(return_value=None)):
        app.dependency_overrides[bl.require_user] = lambda: {"sub": "u1"}
        with TestClient(app) as client:
            resp = client.get("/api/v1/browser-live/targets", params={"agent_id": "not-a-real-agent"})
    assert resp.status_code == 200
    assert [t["id"] for t in resp.json()] == ["T1"]


def test_targets_endpoint_unfiltered_when_no_agent_id_given():
    """No `agent_id` at all must keep the pre-B1 behaviour exactly — this is
    the regression guard for every existing non-agent-scoped call site."""
    app = FastAPI()
    app.include_router(bl.router)
    pages = [
        {"id": "T1", "title": "a", "url": "https://a", "type": "page"},
        {"id": "T2", "title": "b", "url": "https://b", "type": "page"},
    ]
    with patch.object(bl, "_list_page_targets", new=AsyncMock(return_value=pages)), \
         patch.object(bl, "_agent_slug", new=AsyncMock(side_effect=AssertionError("must not be called"))):
        app.dependency_overrides[bl.require_user] = lambda: {"sub": "u1"}
        with TestClient(app) as client:
            resp = client.get("/api/v1/browser-live/targets")
    assert resp.status_code == 200
    assert {t["id"] for t in resp.json()} == {"T1", "T2"}


# ── WS end-to-end: scoped panel shows only the agent's tab ───────────────

def test_ws_scopes_targets_message_to_the_resolved_agent(real_run_target_watcher):
    world = FakeCDPWorld([_page("sparky-tab"), _page("boss-tab")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value={"sparky-tab"})):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "agent-scope@mc.local")
            # Seed the agent on the TestClient's own portal loop (see
            # `_seed_user_and_token`'s docstring for why that matters with
            # the StaticPool test engine).
            import uuid as _uuid

            agent_uuid = _uuid.uuid4()

            async def _seed():
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    s.add(Agent(id=agent_uuid, name="Sparky", slug="sparky", agent_runtime="cli-bridge"))
                    await s.commit()

            client.portal.call(_seed)

            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}") as ws:
                msg = _recv_until(ws, lambda m: m.get("type") == "targets")
                assert [t["id"] for t in msg["targets"]] == ["sparky-tab"]


def test_ws_sabotage_removing_filter_shows_both_tabs(real_run_target_watcher):
    """Sabotage probe: patching `_gateway_owned_ids` to return None (as if
    the filter were disabled/broken) must flip the previous test's
    assertion to seeing BOTH tabs — proving it actually depends on the
    filter being applied."""
    world = FakeCDPWorld([_page("sparky-tab"), _page("boss-tab")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value=None)):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "agent-scope-sabotage@mc.local")
            import uuid as _uuid

            agent_uuid = _uuid.uuid4()

            async def _seed():
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    s.add(Agent(id=agent_uuid, name="Sparky", slug="sparky", agent_runtime="cli-bridge"))
                    await s.commit()

            client.portal.call(_seed)

            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}") as ws:
                msg = _recv_until(ws, lambda m: m.get("type") == "targets" and len(m.get("targets", [])) == 2)
                assert {t["id"] for t in msg["targets"]} == {"sparky-tab", "boss-tab"}
