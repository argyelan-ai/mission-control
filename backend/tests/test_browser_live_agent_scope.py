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
    _recv_with_timeout,
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
        s.add(Agent(id=agent_id, name="Alpha Two", slug="alpha-2", agent_runtime="cli-bridge"))
        await s.commit()
    with patch.object(bl, "async_session_maker", lambda: AsyncSession(test_engine, expire_on_commit=False)):
        assert await bl._agent_slug(str(agent_id)) == "alpha-2"


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
    rows = [{"targetId": "T1", "agent": "alpha"}, {"targetId": "T2", "agent": "alpha"}]

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
            assert params == {"agent": "alpha"}
            return _FakeResp()

    with patch("httpx.AsyncClient", lambda **kw: _FakeClient()):
        ids = await bl._gateway_owned_ids("alpha")
    assert ids == {"T1", "T2"}


@pytest.mark.asyncio
async def test_gateway_owned_ids_returns_none_when_gateway_unreachable():
    bl._gateway_last_reachable.pop("alpha", None)
    with patch("httpx.AsyncClient", side_effect=OSError("refused")):
        ids = await bl._gateway_owned_ids("alpha")
    assert ids is None


@pytest.mark.asyncio
async def test_gateway_unreachable_logs_once_not_on_every_poll(caplog):
    """Low finding: every open scoped panel polls `_gateway_owned_ids` on a
    roughly 1.5s cadence; during a real outage that is one `logger.info` line
    per panel per poll, flooding the log for as long as the panel stays
    open. Must log only on the up→down transition, not on every call while
    it stays down — and must log again once it comes back."""
    bl._gateway_last_reachable.pop("flaky-agent", None)
    with caplog.at_level("INFO", logger="mc.browser_live"):
        with patch("httpx.AsyncClient", side_effect=OSError("refused")):
            for _ in range(5):
                assert await bl._gateway_owned_ids("flaky-agent") is None
        # 5 calls while down → exactly 1 log line, not 5.
        down_logs = [r for r in caplog.records if "cdp-gateway unreachable" in r.message]
        assert len(down_logs) == 1

        class _FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return []

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None):
                return _FakeResp()

        caplog.clear()
        with patch("httpx.AsyncClient", lambda **kw: _FakeClient()):
            ids = await bl._gateway_owned_ids("flaky-agent")
        assert ids == set()
        recovered_logs = [r for r in caplog.records if "reachable again" in r.message]
        assert len(recovered_logs) == 1


# ── /targets REST endpoint ───────────────────────────────────────────────

def test_targets_endpoint_filters_by_agent_when_gateway_knows_the_agent():
    app = FastAPI()
    app.include_router(bl.router)
    pages = [
        {"id": "T1", "title": "alpha's page", "url": "https://a", "type": "page"},
        {"id": "T2", "title": "beta's page", "url": "https://b", "type": "page"},
    ]
    with patch.object(bl, "_list_page_targets", new=AsyncMock(return_value=pages)), \
         patch.object(bl, "_agent_slug", new=AsyncMock(return_value="alpha")), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value={"T1"})), \
         patch("app.auth.require_user", return_value={"sub": "u1"}):
        app.dependency_overrides[bl.require_user] = lambda: {"sub": "u1"}
        with TestClient(app) as client:
            resp = client.get("/api/v1/browser-live/targets", params={"agent_id": str(uuid.uuid4())})
    assert resp.status_code == 200
    body = resp.json()
    assert [t["id"] for t in body["targets"]] == ["T1"]
    assert body["scopeUnavailable"] is False


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
         patch.object(bl, "_agent_slug", new=AsyncMock(return_value="alpha")), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value=None)):
        app.dependency_overrides[bl.require_user] = lambda: {"sub": "u1"}
        with TestClient(app) as client:
            resp = client.get("/api/v1/browser-live/targets", params={"agent_id": str(uuid.uuid4())})
    assert resp.status_code == 200
    body = resp.json()
    assert {t["id"] for t in body["targets"]} == {"T1", "T2"}
    # `agent_id` WAS given but the gateway is down — attribution is
    # unavailable, not "this agent owns nothing" (finding: no signal at all
    # used to reach the caller here).
    assert body["scopeUnavailable"] is True


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
    body = resp.json()
    assert [t["id"] for t in body["targets"]] == ["T1"]
    assert body["scopeUnavailable"] is True


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
    body = resp.json()
    assert {t["id"] for t in body["targets"]} == {"T1", "T2"}
    assert body["scopeUnavailable"] is False


# ── WS end-to-end: scoped panel shows only the agent's tab ───────────────

def test_ws_scopes_targets_message_to_the_resolved_agent(real_run_target_watcher):
    world = FakeCDPWorld([_page("alpha-tab"), _page("beta-tab")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value={"alpha-tab"})):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "agent-scope@mc.local")
            # Seed the agent on the TestClient's own portal loop (see
            # `_seed_user_and_token`'s docstring for why that matters with
            # the StaticPool test engine).
            import uuid as _uuid

            agent_uuid = _uuid.uuid4()

            async def _seed():
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    s.add(Agent(id=agent_uuid, name="Alpha", slug="alpha", agent_runtime="cli-bridge"))
                    await s.commit()

            client.portal.call(_seed)

            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}") as ws:
                msg = _recv_until(ws, lambda m: m.get("type") == "targets")
                assert [t["id"] for t in msg["targets"]] == ["alpha-tab"]


def test_ws_sabotage_removing_filter_shows_both_tabs(real_run_target_watcher):
    """Sabotage probe: patching `_gateway_owned_ids` to return None (as if
    the filter were disabled/broken) must flip the previous test's
    assertion to seeing BOTH tabs — proving it actually depends on the
    filter being applied."""
    world = FakeCDPWorld([_page("alpha-tab"), _page("beta-tab")])
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
                    s.add(Agent(id=agent_uuid, name="Alpha", slug="alpha", agent_runtime="cli-bridge"))
                    await s.commit()

            client.portal.call(_seed)

            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}") as ws:
                msg = _recv_until(ws, lambda m: m.get("type") == "targets" and len(m.get("targets", [])) == 2)
                assert {t["id"] for t in msg["targets"]} == {"alpha-tab", "beta-tab"}


# ── WS end-to-end: scoped panel streams and FOLLOWS only the agent's own
#    tab, even when a foreign tab is newer/active ─────────────────────────
#
# Review finding: mutating `attach_and_stream` to read the unfiltered
# `watcher.targets()` / `watcher.active_id()` instead of the scoped
# `_visible_pages()` left all 44 pre-existing browser_live tests green — none
# of them ever opened a scoped connection with TWO tabs where the foreign one
# is the active one. These two tests close that gap.

def test_ws_scoped_stream_attaches_to_own_tab_not_the_newer_foreign_one(real_run_target_watcher):
    world = FakeCDPWorld([_page("own-tab"), _page("foreign-tab")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value={"own-tab"})):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "scoped-stream@mc.local")
            agent_uuid = uuid.uuid4()

            async def _seed():
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    s.add(Agent(id=agent_uuid, name="Alpha", slug="alpha", agent_runtime="cli-bridge"))
                    await s.commit()

            client.portal.call(_seed)

            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}&follow=1") as ws:
                attached = _recv_until(ws, lambda m: m.get("type") == "attached")
                assert attached["target"]["id"] == "own-tab"
                # Drain the steady-state `targets` push that follows the
                # initial attach, so the quiet-check below only sees NEW
                # messages caused by the foreign tab's navigation.
                initial = _recv_until(ws, lambda m: m.get("type") == "targets")
                assert initial["targets"] == [{"id": "own-tab", "title": "own-tab", "url": "https://example.com"}]

                # The foreign tab becomes active (a navigation bumps its
                # last_active_at past the own tab's) — TargetWatcher's
                # UNFILTERED active_id() would now pick "foreign-tab". The
                # scoped panel must neither attach to it nor report it as
                # `activeId` in the `targets` push.
                world.set_pages([
                    _page("own-tab"),
                    _page("foreign-tab", url="https://foreign.example/new"),
                ])
                events.push("targetInfoChanged", {"targetInfo": {
                    "targetId": "foreign-tab", "type": "page", "title": "foreign-tab",
                    "url": "https://foreign.example/new",
                }})

                # Correctly scoped, the navigation is invisible to this
                # agent's panel (own-tab's own `targets` signature is
                # unchanged) and follow has nothing new to jump to — the
                # handler goes quiet. A `TimeoutError` here is the PASS case;
                # any message at all (an `attached` for foreign-tab, or a
                # `targets` push reporting it) is the sabotage flipping red.
                try:
                    extra = _recv_with_timeout(ws, 0.5)
                except TimeoutError:
                    extra = None
                assert extra is None, f"unexpected message after a foreign tab's navigation: {extra}"

                # No reconnect to the foreign tab ever happened, and the
                # original own-tab stream is still the one that was started.
                assert "foreign-tab" not in world.conns
                assert any(
                    m["method"] == "Page.startScreencast" for m in world.conns["own-tab"].sent
                )


# ── scope_unavailable status: the panel must say so when it silently falls
#    back to showing everything ───────────────────────────────────────────

def test_ws_sends_scope_unavailable_status_when_gateway_is_down(real_run_target_watcher):
    """`agent_id` was given, the agent resolves fine, but `cdp-gateway`
    itself is unreachable — the panel falls back to showing every tab
    (never a harder failure), but must tell the client it did, so the
    toolbar toggle and an i18n hint can say "attribution unavailable"
    instead of silently looking scoped when it is not (review finding)."""
    world = FakeCDPWorld([_page("own-tab")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value=None)):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "scope-unavailable@mc.local")
            agent_uuid = uuid.uuid4()

            async def _seed():
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    s.add(Agent(id=agent_uuid, name="Alpha", slug="alpha", agent_runtime="cli-bridge"))
                    await s.commit()

            client.portal.call(_seed)

            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}") as ws:
                msg = _recv_until(
                    ws, lambda m: m.get("type") == "status" and m.get("code") == "scope_unavailable"
                )
                assert msg["active"] is True


def test_ws_scoped_panel_picks_up_a_brand_new_tab_without_another_tab_event(real_run_target_watcher):
    """Medium finding: `targetCreated` reaches the watcher before cdp-gateway
    has necessarily caught up with its own bookkeeping for that same tab, so
    the FIRST owned-ids fetch right after the watcher's change-triggered
    cache invalidation can still legitimately return the OLD set. Without a
    bounded re-check, nothing looks again until some later, unrelated tab
    event happens to fire — the scoped panel then drops the agent's new tab
    the same way A1 fixed for the unscoped one.

    Simulated here: `_gateway_owned_ids` returns the old (pre-create) set on
    its first call, then the new (post-create) set on every call after —
    with NO further watcher/tab events following the `targetCreated`, the
    new tab must still show up in a `targets` push and become `followedId`
    (follow=1, it is the newest/active tab)."""
    world = FakeCDPWorld([_page("own-tab")])
    events = EventBus()
    app = _make_app(world, events)

    # Call 1 is the panel's pre-create fetch (just "own-tab", correct either
    # way). Call 2 is the fetch `invalidate()` triggers right when
    # `targetCreated` arrives — made to STILL return the old set here, on
    # purpose, so the test actually exercises the race (gateway hasn't
    # caught up with its own createTarget bookkeeping yet) rather than the
    # easy case where the very next fetch already has the answer. Only from
    # call 3 on (which nothing but the bounded re-check timeout can trigger,
    # since no further tab/watcher event follows the one `targetCreated`)
    # does the gateway report the new tab.
    calls = {"n": 0}

    async def _owned_ids(slug):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"own-tab"}
        return {"own-tab", "new-tab"}

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=_owned_ids):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "scoped-new-tab@mc.local")
            agent_uuid = uuid.uuid4()

            async def _seed():
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    s.add(Agent(id=agent_uuid, name="Alpha", slug="alpha", agent_runtime="cli-bridge"))
                    await s.commit()

            client.portal.call(_seed)

            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}&follow=1") as ws:
                _recv_until(ws, lambda m: m.get("type") == "attached")
                _recv_until(ws, lambda m: m.get("type") == "targets")

                # The agent opens a brand-new tab. cdp-gateway hasn't caught
                # up with it yet (our first post-invalidate fetch above still
                # returns the old set) — exactly one `targetCreated` event,
                # nothing else follows it.
                world.set_pages([_page("own-tab"), _page("new-tab")])
                events.push("targetCreated", {"targetInfo": {
                    "targetId": "new-tab", "type": "page", "title": "new-tab",
                    "url": "https://example.com",
                }})

                msg = _recv_until(
                    ws,
                    lambda m: m.get("type") == "targets" and "new-tab" in {t["id"] for t in m.get("targets", [])},
                    timeout=5.0,
                )
                ids = {t["id"] for t in msg["targets"]}
                assert ids == {"own-tab", "new-tab"}
                assert msg["activeId"] == "new-tab"
                assert msg["followedId"] == "new-tab"


def test_ws_sends_scope_unavailable_false_on_first_check_even_though_value_is_the_default(
    real_run_target_watcher,
):
    """Medium finding: the server used to only send `scope_unavailable` on a
    CHANGE from its internal initial value (False) — so a connection where
    attribution is fine from the very first check (the common, happy case)
    never got any status at all. The frontend side of this finding relies on
    the client always seeing at least one `scope_unavailable` status once
    `agent_id` was given, so it can stop trusting a stale REST
    `scopeUnavailable: true` the moment the WS says otherwise. This is the
    backend half: the very first loop iteration must send the status
    regardless of whether the computed value equals the default."""
    world = FakeCDPWorld([_page("own-tab")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value={"own-tab"})):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "scope-first-ok@mc.local")
            agent_uuid = uuid.uuid4()

            async def _seed():
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    s.add(Agent(id=agent_uuid, name="Alpha", slug="alpha", agent_runtime="cli-bridge"))
                    await s.commit()

            client.portal.call(_seed)

            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}") as ws:
                msg = _recv_until(
                    ws, lambda m: m.get("type") == "status" and m.get("code") == "scope_unavailable"
                )
                assert msg["active"] is False


def test_ws_does_not_send_scope_unavailable_when_not_scoped(real_run_target_watcher):
    """No `agent_id` at all must never emit this status — a plain, unscoped
    panel has nothing to say "unavailable" about (regression guard for the
    previous test's own assertion: without this, `active: True` could just
    be the status every connection gets, proving nothing)."""
    world = FakeCDPWorld([_page("a")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "no-scope@mc.local")
            with _ws(client, f"/api/v1/browser-live/ws?token={token}") as ws:
                _recv_until(ws, lambda m: m.get("type") == "attached")
                try:
                    extra = _recv_with_timeout(ws, 0.3)
                    assert not (extra.get("type") == "status" and extra.get("code") == "scope_unavailable")
                except TimeoutError:
                    pass




# ── unassigned fallback (live finding 04.10.2026) ────────────────────────
#
# Live, every tab in the shared browser came back `agent: null` from
# cdp-gateway, so a scoped panel (opened from an agent's chat) told the
# operator "<agent> has no open tab right now" while that agent HAD a tab
# open — just not one the gateway could attribute. Tabs nobody is assigned
# to (playwright-mcp's — shared by every claude agent — or a tab no
# identified agent has claimed yet) can't be ruled out as "this agent's".
# Rule: scoped list empty + unassigned tabs exist → show every tab, flag the
# unassigned ones, and say so. Only "every tab belongs to some OTHER agent"
# is a real "no open tab".

def _get_targets(pages, *, owned, assigned):
    app = FastAPI()
    app.include_router(bl.router)
    with patch.object(bl, "_list_page_targets", new=AsyncMock(return_value=pages)), \
         patch.object(bl, "_agent_slug", new=AsyncMock(return_value="alpha")), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value=owned)), \
         patch.object(bl, "_gateway_assigned_ids", new=AsyncMock(return_value=assigned)):
        app.dependency_overrides[bl.require_user] = lambda: {"sub": "u1"}
        with TestClient(app) as client:
            resp = client.get("/api/v1/browser-live/targets", params={"agent_id": str(uuid.uuid4())})
    assert resp.status_code == 200
    return resp.json()


_TWO_PAGES = [
    {"id": "FREE", "title": "unassigned page", "url": "https://free.example", "type": "page"},
    {"id": "BETA", "title": "beta's page", "url": "https://beta.example", "type": "page"},
]


def test_targets_empty_scope_with_unassigned_tabs_falls_back_to_all_tabs():
    body = _get_targets(_TWO_PAGES, owned=set(), assigned={"BETA"})
    assert [t["id"] for t in body["targets"]] == ["FREE", "BETA"]
    flags = {t["id"]: t.get("unassigned", False) for t in body["targets"]}
    assert flags == {"FREE": True, "BETA": False}
    assert body["unassignedFallback"] is True
    assert body["unassignedCount"] == 1
    assert body["scopeUnavailable"] is False


def test_targets_a_tab_the_gateway_never_saw_counts_as_unassigned():
    """Chromium lists it, the gateway has no row for it at all (just opened,
    or the gateway restarted): nobody is known to own it."""
    body = _get_targets(_TWO_PAGES, owned=set(), assigned=set())
    assert body["unassignedFallback"] is True
    assert body["unassignedCount"] == 2


def test_targets_every_tab_owned_by_another_agent_is_a_real_empty_scope():
    body = _get_targets(_TWO_PAGES, owned=set(), assigned={"FREE", "BETA"})
    assert body["targets"] == []
    assert body["unassignedFallback"] is False


def test_targets_own_tabs_win_over_the_fallback():
    body = _get_targets(_TWO_PAGES, owned={"FREE"}, assigned={"FREE"})
    assert [t["id"] for t in body["targets"]] == ["FREE"]
    assert body["unassignedFallback"] is False
    assert "unassigned" not in body["targets"][0]


def test_targets_unknown_assignment_never_claims_a_fallback():
    """Owned set answered, the all-tabs query failed — we can't tell, so we
    don't pretend: the plain scoped (empty) view, no fallback flag."""
    body = _get_targets(_TWO_PAGES, owned=set(), assigned=None)
    assert body["targets"] == []
    assert body["unassignedFallback"] is False


def test_targets_gateway_down_is_scope_unavailable_not_fallback():
    body = _get_targets(_TWO_PAGES, owned=None, assigned=None)
    assert body["scopeUnavailable"] is True
    assert body["unassignedFallback"] is False
    assert {t["id"] for t in body["targets"]} == {"FREE", "BETA"}


@pytest.mark.asyncio
async def test_gateway_assigned_ids_reads_every_row_with_an_agent():
    rows = [
        {"targetId": "T1", "agent": "alpha"},
        {"targetId": "T2", "agent": None},
        {"targetId": "T3", "agent": "beta"},
    ]

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
            assert url.endswith("/mc/targets")
            assert not params  # every agent's rows, not one agent's
            return _FakeResp()

    with patch("httpx.AsyncClient", lambda **kw: _FakeClient()):
        assert await bl._gateway_assigned_ids() == {"T1", "T3"}
    with patch("httpx.AsyncClient", side_effect=OSError("refused")):
        assert await bl._gateway_assigned_ids() is None


def _seed_alpha(client):
    agent_uuid = uuid.uuid4()

    async def _seed():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            s.add(Agent(id=agent_uuid, name="Alpha", slug="alpha", agent_runtime="cli-bridge"))
            await s.commit()

    client.portal.call(_seed)
    return agent_uuid


def test_ws_empty_scope_with_an_unassigned_tab_streams_it_with_a_fallback_status(real_run_target_watcher):
    world = FakeCDPWorld([_page("free-tab")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value=set())), \
         patch.object(bl, "_gateway_assigned_ids", new=AsyncMock(return_value=set())):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "fallback-ws@mc.local")
            agent_uuid = _seed_alpha(client)
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}") as ws:
                status = _recv_until(
                    ws, lambda m: m.get("type") == "status" and m.get("code") == "unassigned_fallback"
                )
                assert status["active"] is True
                assert status["count"] == 1
                attached = _recv_until(ws, lambda m: m.get("type") == "attached")
                assert attached["target"]["id"] == "free-tab"
                msg = _recv_until(ws, lambda m: m.get("type") == "targets" and m.get("targets"))
                assert msg["targets"] == [{
                    "id": "free-tab", "title": "free-tab", "url": "https://example.com", "unassigned": True,
                }]


def test_ws_fallback_ends_once_the_agent_owns_a_tab(real_run_target_watcher):
    world = FakeCDPWorld([_page("free-tab")])
    events = EventBus()
    app = _make_app(world, events)
    owned = {"value": set()}

    async def _owned(slug):
        return set(owned["value"])

    async def _assigned():
        return set(owned["value"])

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=_owned), \
         patch.object(bl, "_gateway_assigned_ids", new=_assigned):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "fallback-ends@mc.local")
            agent_uuid = _seed_alpha(client)
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}") as ws:
                _recv_until(ws, lambda m: m.get("type") == "status" and m.get("code") == "unassigned_fallback"
                            and m.get("active") is True)
                owned["value"] = {"free-tab"}  # the agent claimed/navigated it
                done = _recv_until(
                    ws,
                    lambda m: m.get("type") == "status" and m.get("code") == "unassigned_fallback"
                    and m.get("active") is False,
                    timeout=5.0,
                )
                assert done["count"] == 0
                msg = _recv_until(ws, lambda m: m.get("type") == "targets"
                                  and m.get("targets") and "unassigned" not in m["targets"][0], timeout=5.0)
                assert [t["id"] for t in msg["targets"]] == ["free-tab"]


def test_ws_every_tab_foreign_stays_a_real_no_page(real_run_target_watcher):
    """Sabotage guard for the rule's other half: the fallback must not fire
    when every tab is attributed to some other agent."""
    world = FakeCDPWorld([_page("beta-tab")])
    events = EventBus()
    app = _make_app(world, events)

    with patch("websockets.connect", world.connect), \
         patch.object(bl, "_gateway_owned_ids", new=AsyncMock(return_value=set())), \
         patch.object(bl, "_gateway_assigned_ids", new=AsyncMock(return_value={"beta-tab"})):
        with TestClient(app, raise_server_exceptions=True) as client:
            token = _seed_user_and_token(client, "fallback-foreign@mc.local")
            agent_uuid = _seed_alpha(client)
            with _ws(client, f"/api/v1/browser-live/ws?token={token}&agent_id={agent_uuid}") as ws:
                status = _recv_until(
                    ws, lambda m: m.get("type") == "status" and m.get("code") == "unassigned_fallback"
                )
                assert status["active"] is False
                _recv_until(ws, lambda m: m.get("type") == "status" and m.get("code") == "no_page")
